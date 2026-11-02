"""Telegram handlers. Thin on purpose: the conversation logic lives in `assistant`."""

import asyncio
import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.utils import timezone
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from chatbot.models import Conversation, Prospect

from . import ai, identity, office_hours, prompts
from .assistant import Assistant
from .config import BotConfig, load_config
from .notify import advisor_summary

log = logging.getLogger(__name__)

GREETINGS = {'/start', '/clear', 'hola', 'hi', 'hello', 'buenas', 'buenos dias', 'buenos días', 'buenas tardes', 'hey'}
ENGLISH_HINTS = ('hello', 'hi', 'hey', 'how', 'english')
ENGLISH_WELCOME = 'Hello! 🌴 Thank you for contacting Paradise Tour Travel Agency. How can we help you today?'

REENGAGE_AFTER = timedelta(minutes=15)
REENGAGE_NUDGES = 3  # then one farewell
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _client(context: ContextTypes.DEFAULT_TYPE, config: BotConfig):
    return ai.client_for(config, context.application.bot_data.setdefault('ai_clients', {}))


def _conversation(telegram_id: str, chat_id: int) -> Conversation:
    conversation, _ = Conversation.objects.get_or_create(telegram_id=telegram_id, defaults={'chat_id': chat_id})
    return conversation


async def _download(context: ContextTypes.DEFAULT_TYPE, message) -> identity.Upload | None:
    if message.photo:
        photo = message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        return identity.Upload(photo.file_id, 'ine.jpg', bytes(await file.download_as_bytearray()), 'image/jpeg')
    document = message.document
    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        return None
    file = await context.bot.get_file(document.file_id)
    return identity.Upload(
        document.file_id,
        document.file_name or 'identificacion',
        bytes(await file.download_as_bytearray()),
        document.mime_type or 'application/octet-stream',
    )


async def _notify_advisors(context: ContextTypes.DEFAULT_TYPE, config: BotConfig, prospect: Prospect) -> None:
    if not config.advisor_chat_id or prospect.advisor_notified_at:
        return
    try:
        await context.bot.send_message(chat_id=config.advisor_chat_id, text=advisor_summary(prospect))
    except Exception:
        log.exception('could not notify the advisors about prospect %s', prospect.pk)
        return
    await sync_to_async(Prospect.objects.filter(pk=prospect.pk).update)(advisor_notified_at=timezone.now())


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    telegram_id = str(update.effective_user.id)
    text = (message.text or '').strip()
    lowered = text.lower()

    config = await sync_to_async(load_config)()
    if not config.ai_ready:
        await message.reply_text('⚠️ Falta configurar el modelo de IA: una API key o AI_BASE_URL en el panel o en el archivo .env.')
        return
    client = _client(context, config)

    conversation = await sync_to_async(_conversation)(telegram_id, message.chat_id)
    if lowered in ('/start', '/clear'):
        conversation.messages = []
    conversation.chat_id = message.chat_id
    conversation.is_finished = False
    conversation.reengagement_attempts = 0
    conversation.last_user_message_at = timezone.now()

    if not conversation.messages:
        english = any(hint in lowered.split() for hint in ENGLISH_HINTS)
        welcome = ENGLISH_WELCOME if english else config.welcome_message
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(1.2)
        await message.reply_text(welcome)
        conversation.messages = [{'role': 'assistant', 'content': welcome}]
        if lowered in GREETINGS:
            await sync_to_async(conversation.save)()
            return

    user_content = text
    if message.photo or message.document:
        upload = await _download(context, message)
        if upload is None:
            await message.reply_text('El archivo es demasiado grande. Envía una foto de tu identificación, por favor.')
            return
        await sync_to_async(identity.store)(telegram_id, upload)
        user_content = await identity.describe_for_model(client, config.vision_model, upload, message.caption or '')
    if not user_content:
        user_content = '[Multimedia no compatible]'

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        turn = await Assistant(client, config).respond(telegram_id, conversation.messages, user_content, timezone.localtime())
    except Exception:
        log.exception('the assistant failed for %s', telegram_id)
        await sync_to_async(conversation.save)()
        await message.reply_text('Lo siento, estoy teniendo un problema de conexión. Inténtalo de nuevo en un momento.')
        return

    conversation.messages = turn.messages
    conversation.is_finished = turn.finished
    await sync_to_async(conversation.save)()
    # Plain text: model output with unbalanced Markdown used to make Telegram reject the whole reply.
    await message.reply_text(turn.reply)

    # Read back rather than trusting the tool: the ID can be the last missing piece, and it is stored outside it.
    prospect = await Prospect.objects.filter(telegram_id=telegram_id).afirst()
    if prospect and prospect.is_complete:
        await _notify_advisors(context, config, prospect)


def _due_conversations(now) -> list[Conversation]:
    return list(
        Conversation.objects.filter(
            channel=Conversation.Channel.TELEGRAM,
            is_finished=False,
            last_user_message_at__isnull=False,
            reengagement_attempts__lte=REENGAGE_NUDGES,
            updated_at__lte=now - REENGAGE_AFTER,
        ).order_by('updated_at')[:20]
    )


async def reengage(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Nudges travellers who went quiet: three short messages 15 minutes apart,
    then a farewell. Driven from the database, so it survives restarts, and it
    never writes at night.
    """
    now = timezone.localtime()  # the agency's wall clock, for quiet hours and the prompt
    if office_hours.is_quiet_time(now):
        return
    config = await sync_to_async(load_config)()
    if not config.ai_ready:
        return
    assistant = Assistant(_client(context, config), config)

    for conversation in await sync_to_async(_due_conversations)(timezone.now()):
        attempt = conversation.reengagement_attempts + 1
        farewell = attempt > REENGAGE_NUDGES
        instruction = prompts.FAREWELL if farewell else prompts.REENGAGE.format(
            attempt=attempt, total=REENGAGE_NUDGES, minutes=int(REENGAGE_AFTER.total_seconds() // 60) * attempt)
        try:
            text = await assistant.follow_up(conversation.messages, instruction, now)
            if text:
                await context.bot.send_message(chat_id=conversation.chat_id, text=text)
                conversation.messages = [*conversation.messages, {'role': 'assistant', 'content': text}]
        except Exception:
            log.exception('could not re-engage %s', conversation.telegram_id)
        conversation.reengagement_attempts = attempt
        conversation.is_finished = farewell
        await sync_to_async(conversation.save)()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error('telegram handler error', exc_info=context.error)


def build_application(token: str) -> Application:
    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler(['start', 'clear'], on_message))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    application.add_error_handler(on_error)
    application.job_queue.run_repeating(reengage, interval=60, first=30, name='reengage')
    return application
