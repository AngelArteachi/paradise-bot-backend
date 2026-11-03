from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from chatbot.bot import telegram_app
from chatbot.models import BotSetting, Conversation, Prospect

from .fakes import FakeOpenAI, text, tool_call


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_chat_action(self, chat_id, action):
        pass


class FakeMessage:
    def __init__(self, text, chat_id=555):
        self.text = text
        self.chat_id = chat_id
        self.photo = []
        self.document = None
        self.caption = None
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def update_for(message, user_id=555):
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id))


def context_for(bot):
    return SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={}))


@override_settings(TIME_ZONE='America/Cancun')
class TelegramHandlerTest(TestCase):
    def setUp(self):
        BotSetting.objects.create(openai_api_key='sk-test', advisor_chat_id='-1001', welcome_message='¡Hola! Bienvenido.')
        sleep = mock.patch('chatbot.bot.telegram_app.asyncio.sleep', new=mock.AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)

    def use_model(self, *responses):
        fake = FakeOpenAI(*responses)
        patcher = mock.patch('chatbot.bot.ai.AsyncOpenAI', return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    async def test_a_greeting_gets_the_welcome_and_nothing_else(self):
        self.use_model()
        message = FakeMessage('hola')
        await telegram_app.on_message(update_for(message), context_for(FakeBot()))
        self.assertEqual(message.replies, ['¡Hola! Bienvenido.'])
        conversation = await Conversation.objects.aget(telegram_id='555')
        self.assertEqual(conversation.messages, [{'role': 'assistant', 'content': '¡Hola! Bienvenido.'}])

    async def test_the_conversation_survives_between_messages(self):
        fake = self.use_model(text('¿Cuál es tu nombre?'), text('Mucho gusto, Ana.'))
        bot = FakeBot()
        await telegram_app.on_message(update_for(FakeMessage('Hola, quiero viajar')), context_for(bot))
        second = FakeMessage('Ana')
        await telegram_app.on_message(update_for(second), context_for(bot))
        self.assertEqual(second.replies, ['Mucho gusto, Ana.'])
        # The second request contains the first exchange, read back from the database.
        sent = [m.get('content') for m in fake.requests[1]['messages']]
        self.assertIn('Hola, quiero viajar', sent)
        self.assertIn('¿Cuál es tu nombre?', sent)

    async def test_advisors_are_notified_once_when_a_prospect_completes(self):
        await Prospect.objects.acreate(telegram_id='555', ine_file_id='tg-file')
        complete = {'name': 'Ana López', 'phone': '+529981234567', 'email': 'ana@example.com', 'destination': 'Cozumel',
                    'dates': 'Semana Santa', 'nights': '4', 'passengers_adult': 2}
        self.use_model(text('Hola'), tool_call('save_prospect_info', complete), text('Un asesor te llamará.'),
                       tool_call('save_prospect_info', {'nights': '5'}), text('Actualizado.'))
        bot = FakeBot()
        for words in ['Hola, quiero cotizar', 'Estos son mis datos, 4 noches', 'Mejor 5 noches']:
            await telegram_app.on_message(update_for(FakeMessage(words)), context_for(bot))
        advisor_messages = [text for chat, text in bot.sent if chat == '-1001']
        self.assertEqual(len(advisor_messages), 1)
        self.assertIn('Cozumel', advisor_messages[0])
        self.assertIsNotNone((await Prospect.objects.aget(telegram_id='555')).advisor_notified_at)

    async def test_without_an_api_key_the_bot_says_so(self):
        await BotSetting.objects.all().aupdate(openai_api_key='')
        message = FakeMessage('hola')
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            await telegram_app.on_message(update_for(message), context_for(FakeBot()))
        self.assertIn('API key', message.replies[0])


@override_settings(TIME_ZONE='America/Cancun')
class ReengagementTest(TestCase):
    def setUp(self):
        BotSetting.objects.create(openai_api_key='sk-test')

    async def quiet_conversation(self, minutes_ago, attempts=0):
        conversation = await Conversation.objects.acreate(
            telegram_id='77', chat_id=77, last_user_message_at=timezone.now(), reengagement_attempts=attempts,
            messages=[{'role': 'user', 'content': 'Hola'}, {'role': 'assistant', 'content': '¿A dónde viajas?'}])
        await Conversation.objects.filter(pk=conversation.pk).aupdate(updated_at=timezone.now() - timedelta(minutes=minutes_ago))
        return conversation

    def at_noon(self):
        noon = datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo('America/Cancun'))
        return mock.patch('chatbot.bot.telegram_app.timezone.localtime', return_value=noon)

    async def test_nudges_a_traveller_who_went_quiet(self):
        await self.quiet_conversation(minutes_ago=20)
        bot = FakeBot()
        with self.at_noon(), mock.patch('chatbot.bot.ai.AsyncOpenAI', return_value=FakeOpenAI(text('¿Sigues ahí?'))):
            await telegram_app.reengage(context_for(bot))
        self.assertEqual(bot.sent, [(77, '¿Sigues ahí?')])
        conversation = await Conversation.objects.aget(telegram_id='77')
        self.assertEqual(conversation.reengagement_attempts, 1)
        self.assertFalse(conversation.is_finished)

    async def test_says_goodbye_after_three_nudges(self):
        await self.quiet_conversation(minutes_ago=20, attempts=3)
        with self.at_noon(), mock.patch('chatbot.bot.ai.AsyncOpenAI', return_value=FakeOpenAI(text('¡Hasta pronto!'))):
            await telegram_app.reengage(context_for(FakeBot()))
        self.assertTrue((await Conversation.objects.aget(telegram_id='77')).is_finished)

    async def test_leaves_recent_conversations_and_nights_alone(self):
        await self.quiet_conversation(minutes_ago=5)
        bot = FakeBot()
        with self.at_noon(), mock.patch('chatbot.bot.ai.AsyncOpenAI', return_value=FakeOpenAI()):
            await telegram_app.reengage(context_for(bot))
        night = datetime(2026, 11, 2, 23, 0, tzinfo=ZoneInfo('America/Cancun'))
        with mock.patch('chatbot.bot.telegram_app.timezone.localtime', return_value=night):
            await telegram_app.reengage(context_for(bot))
        self.assertEqual(bot.sent, [])
