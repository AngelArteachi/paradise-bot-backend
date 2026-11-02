"""
The web chat: the same assistant as on Telegram, in the browser, so anyone can
try it without installing anything. It is a public demo, so it has limits per
visitor and per day, never receives real IDs, and forgets everything after
WEB_CHAT_RETENTION_HOURS.
"""

import json
import logging
import uuid
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .bot import ai, prompts
from .bot.assistant import Assistant
from .bot.config import load_config
from .bot.notify import advisor_summary
from .models import Channel, Conversation, Prospect

log = logging.getLogger(__name__)

SESSION_SALT = 'paradise.web-chat'
SAMPLE_ID_FILE = 'demo-sample'
SAMPLE_ID_MESSAGE = prompts.ID_ACCEPTED.format(data='INE de ejemplo de la demo web', caption='').strip()
SAMPLE_ID_LABEL = '📎 INE de ejemplo'
MAX_MESSAGE_CHARS = 500
PROSPECT_FIELDS = ('name', 'phone', 'email', 'destination', 'dates', 'nights', 'passengers_adult', 'passengers_child')


class LimitReached(Exception):
    pass


def _enabled():
    if not settings.WEB_CHAT_ENABLED:
        raise Http404


def client_ip(request) -> str:
    """The visitor's address. Behind N proxies, the Nth address from the right of X-Forwarded-For."""
    hops = settings.WEB_CHAT_PROXY_HOPS
    forwarded = [part.strip() for part in request.headers.get('X-Forwarded-For', '').split(',') if part.strip()]
    if hops and len(forwarded) >= hops:
        return forwarded[-hops]
    return request.META.get('REMOTE_ADDR', '')


def _count(key: str, limit: int, seconds: int) -> None:
    """Fixed-window counter in the cache. Raises once `limit` is passed."""
    cache.add(key, 0, seconds)
    try:
        used = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, seconds)
        used = 1
    if used > limit:
        raise LimitReached


def check_limits(request, conversation: Conversation) -> None:
    visitor_turns = sum(1 for message in conversation.messages if message.get('role') == 'user')
    if visitor_turns >= settings.WEB_CHAT_MAX_MESSAGES:
        raise LimitReached
    _count(f'webchat:ip:{client_ip(request)}', settings.WEB_CHAT_MAX_PER_IP_PER_HOUR, 60 * 60)
    _count(f'webchat:day:{timezone.localdate()}', settings.WEB_CHAT_DAILY_LIMIT, 24 * 60 * 60)


def purge_expired() -> None:
    cutoff = timezone.now() - timedelta(hours=settings.WEB_CHAT_RETENTION_HOURS)
    Conversation.objects.filter(channel=Channel.WEB, updated_at__lt=cutoff).delete()
    Prospect.objects.filter(channel=Channel.WEB, updated_at__lt=cutoff).delete()


def session_for(conversation: Conversation) -> str:
    return signing.dumps({'id': conversation.telegram_id}, salt=SESSION_SALT)


def conversation_from(session: str) -> Conversation | None:
    try:
        data = signing.loads(session or '', salt=SESSION_SALT, max_age=settings.WEB_CHAT_RETENTION_HOURS * 60 * 60)
    except signing.BadSignature:
        return None
    return Conversation.objects.filter(channel=Channel.WEB, telegram_id=data.get('id')).first()


def visible_messages(conversation: Conversation) -> list[dict]:
    """What the visitor sees: their messages and the assistant's replies, without tool calls."""
    visible = []
    for message in conversation.messages:
        content = message.get('content')
        if message.get('role') not in ('user', 'assistant') or not isinstance(content, str) or not content:
            continue
        if message['role'] == 'user' and content == SAMPLE_ID_MESSAGE:
            content = SAMPLE_ID_LABEL
        visible.append({'role': message['role'], 'content': content})
    return visible


def prospect_payload(conversation: Conversation) -> dict | None:
    prospect = Prospect.objects.filter(telegram_id=conversation.telegram_id).first()
    if prospect is None:
        return None
    payload = {field: getattr(prospect, field) for field in PROSPECT_FIELDS}
    payload.update(
        has_id_document=prospect.has_id_document,
        is_complete=prospect.is_complete,
        stage=prospect.stage,
        stage_label=prospect.get_stage_display(),
    )
    return payload


def state(conversation: Conversation) -> dict:
    turns_left = settings.WEB_CHAT_MAX_MESSAGES - sum(1 for m in conversation.messages if m.get('role') == 'user')
    return {
        'session': session_for(conversation),
        'messages': visible_messages(conversation),
        'prospect': prospect_payload(conversation),
        'finished': conversation.is_finished,
        'messages_left': max(turns_left, 0),
    }


def start_conversation() -> Conversation:
    purge_expired()
    welcome = load_config().welcome_message
    return Conversation.objects.create(
        telegram_id=f'web-{uuid.uuid4().hex[:24]}', channel=Channel.WEB, chat_id=0,
        messages=[{'role': 'assistant', 'content': welcome}], last_user_message_at=timezone.now())


def store_sample_id(conversation: Conversation) -> None:
    prospect, _ = Prospect.objects.get_or_create(telegram_id=conversation.telegram_id, defaults={'channel': Channel.WEB})
    prospect.ine_file_id = SAMPLE_ID_FILE
    prospect.save(update_fields=['ine_file_id', 'updated_at'])


def demo_page(request):
    _enabled()
    return render(request, 'chatbot/demo.html', {'max_chars': MAX_MESSAGE_CHARS})


@csrf_exempt
def chat_start_view(request):
    _enabled()
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    return JsonResponse(state(start_conversation()), status=201)


@csrf_exempt
def chat_state_view(request):
    _enabled()
    conversation = conversation_from(request.GET.get('session', ''))
    if conversation is None:
        return JsonResponse({'error': 'La sesión expiró. Empieza una conversación nueva.'}, status=404)
    return JsonResponse(state(conversation))


@csrf_exempt
async def chat_message_view(request):
    _enabled()
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:  # also a body that is not UTF-8
        return JsonResponse({'error': 'El cuerpo de la petición no es JSON válido.'}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({'error': 'Se esperaba un objeto JSON.'}, status=400)

    conversation = await sync_to_async(conversation_from)(str(body.get('session', '')))
    if conversation is None:
        return JsonResponse({'error': 'La sesión expiró. Empieza una conversación nueva.'}, status=404)

    sample_id = body.get('sample_id') is True
    text = str(body.get('message') or '').strip()
    if not sample_id and not text:
        return JsonResponse({'error': 'Escribe un mensaje.'}, status=400)
    if len(text) > MAX_MESSAGE_CHARS:
        return JsonResponse({'error': f'El mensaje es muy largo (máximo {MAX_MESSAGE_CHARS} caracteres).'}, status=400)

    config = await sync_to_async(load_config)()
    if not config.ai_ready:
        return JsonResponse({'error': 'La demo no tiene un modelo de IA configurado.'}, status=503)
    try:
        await sync_to_async(check_limits)(request, conversation)
    except LimitReached:
        return JsonResponse({'error': 'La demo llegó a su límite de mensajes por ahora. Vuelve a intentarlo más tarde.'}, status=429)

    if sample_id:
        await sync_to_async(store_sample_id)(conversation)
        text = SAMPLE_ID_MESSAGE

    # A new client per request: under WSGI every async view runs in its own event loop.
    client = ai.new_client(config)
    try:
        turn = await Assistant(client, config, channel=Channel.WEB).respond(
            conversation.telegram_id, conversation.messages, text, timezone.localtime())
    except Exception:
        log.exception('the assistant failed for %s', conversation.telegram_id)
        return JsonResponse({'error': 'El asistente no pudo responder. Inténtalo de nuevo en un momento.'}, status=502)
    finally:
        await client.close()

    conversation.messages = turn.messages
    conversation.is_finished = turn.finished
    conversation.last_user_message_at = timezone.now()
    await sync_to_async(conversation.save)()

    # On Telegram this goes to the advisors' group; the demo shows it next to the chat.
    # As on Telegram, read back: the sample ID can be the last missing piece.
    notification = None
    prospect = await Prospect.objects.filter(telegram_id=conversation.telegram_id, advisor_notified_at__isnull=True).afirst()
    if prospect and prospect.is_complete:
        notification = advisor_summary(prospect)
        await Prospect.objects.filter(pk=prospect.pk).aupdate(advisor_notified_at=timezone.now())

    payload = await sync_to_async(state)(conversation)
    payload['reply'] = turn.reply
    payload['advisor_notification'] = notification
    return JsonResponse(payload)
