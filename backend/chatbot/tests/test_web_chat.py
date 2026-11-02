import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from chatbot import web_chat
from chatbot.bot.config import load_config
from chatbot.bot.telegram_app import _due_conversations
from chatbot.models import BotSetting, Channel, Conversation, Prospect

from .fakes import FakeOpenAI, text, tool_call

COMPLETE = {'name': 'Ana López', 'phone': '+529981234567', 'email': 'ana@example.com', 'destination': 'Cozumel',
            'dates': 'Semana Santa', 'nights': '4', 'passengers_adult': 2}


class ProviderConfigTest(TestCase):
    def test_a_local_endpoint_needs_no_key(self):
        env = {'AI_BASE_URL': 'http://localhost:11434/v1', 'AI_MODEL': 'qwen2.5:7b', 'AI_API_KEY': '', 'OPENAI_API_KEY': ''}
        with mock.patch.dict('os.environ', env):
            config = load_config()
        self.assertTrue(config.ai_ready)
        self.assertEqual(config.model, 'qwen2.5:7b')

    def test_without_key_or_endpoint_the_ai_is_not_ready(self):
        with mock.patch.dict('os.environ', {'AI_BASE_URL': '', 'AI_API_KEY': '', 'OPENAI_API_KEY': ''}):
            self.assertFalse(load_config().ai_ready)


@override_settings(WEB_CHAT_ENABLED=True, WEB_CHAT_MAX_MESSAGES=30, WEB_CHAT_MAX_PER_IP_PER_HOUR=60,
                   WEB_CHAT_DAILY_LIMIT=1000, WEB_CHAT_RETENTION_HOURS=24, WEB_CHAT_PROXY_HOPS=0)
class WebChatTest(TestCase):
    def setUp(self):
        cache.clear()
        BotSetting.objects.create(openai_api_key='sk-test', welcome_message='¡Hola! Soy el asistente.')

    def use_model(self, *responses):
        fake = FakeOpenAI(*responses)
        patcher = mock.patch('chatbot.bot.ai.AsyncOpenAI', return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def start(self):
        response = self.client.post('/api/chat/')
        self.assertEqual(response.status_code, 201)
        return response.json()

    def say(self, session, **body):
        return self.client.post('/api/chat/messages/', json.dumps({'session': session, **body}), content_type='application/json')

    def test_the_page_loads(self):
        self.assertContains(self.client.get('/'), 'Asistente de ventas de Paradise Tour')

    @override_settings(WEB_CHAT_ENABLED=False)
    def test_the_demo_can_be_turned_off(self):
        self.assertEqual(self.client.get('/').status_code, 404)
        self.assertEqual(self.client.post('/api/chat/').status_code, 404)

    def test_a_new_session_starts_with_the_welcome(self):
        state = self.start()
        self.assertEqual(state['messages'], [{'role': 'assistant', 'content': '¡Hola! Soy el asistente.'}])
        self.assertIsNone(state['prospect'])
        self.assertEqual(Conversation.objects.get().channel, Channel.WEB)

    def test_the_assistant_replies_and_saves_the_prospect(self):
        fake = self.use_model(tool_call('save_prospect_info', {'name': 'Ana López', 'destination': 'Cozumel'}), text('¡Gracias, Ana!'))
        session = self.start()['session']
        state = self.say(session, message='Soy Ana López y quiero ir a Cozumel').json()
        self.assertEqual(state['reply'], '¡Gracias, Ana!')
        self.assertEqual(state['prospect']['name'], 'Ana López')
        self.assertEqual(Prospect.objects.get().channel, Channel.WEB)
        # The visitor sees the chat, not the tool calls.
        self.assertEqual([m['role'] for m in state['messages']], ['assistant', 'user', 'assistant'])
        self.assertIn('public web demo', fake.requests[0]['messages'][0]['content'])

    def test_the_sample_id_counts_as_the_document_and_completes_the_profile(self):
        self.use_model(tool_call('save_prospect_info', COMPLETE), text('Ahora tu INE, por favor.'),
                       text('¡Listo! Un asesor te contactará.'))
        session = self.start()['session']
        first = self.say(session, message='Estos son mis datos').json()
        self.assertIsNone(first['advisor_notification'])
        self.assertFalse(first['prospect']['is_complete'])
        state = self.say(session, sample_id=True).json()
        self.assertTrue(state['prospect']['has_id_document'])
        self.assertTrue(state['prospect']['is_complete'])
        self.assertEqual(state['messages'][-2]['content'], web_chat.SAMPLE_ID_LABEL)
        self.assertIn('Cozumel', state['advisor_notification'])

    def test_the_advisors_are_told_once_when_the_profile_completes(self):
        self.use_model(text('Ahora tus datos.'), tool_call('save_prospect_info', COMPLETE), text('¡Listo!'),
                       tool_call('save_prospect_info', {'nights': '5'}), text('Actualizado.'))
        session = self.start()['session']
        self.say(session, sample_id=True)
        complete = self.say(session, message='Estos son mis datos').json()
        self.assertIn('Cozumel', complete['advisor_notification'])
        again = self.say(session, message='Mejor 5 noches').json()
        self.assertIsNone(again['advisor_notification'])

    def test_a_reload_restores_the_conversation(self):
        self.use_model(text('¿A dónde quieres viajar?'))
        session = self.start()['session']
        self.say(session, message='Hola')
        state = self.client.get('/api/chat/state/', {'session': session}).json()
        self.assertEqual(len(state['messages']), 3)

    def test_a_forged_session_is_rejected(self):
        conversation = Conversation.objects.create(telegram_id='555', chat_id=555)
        self.assertEqual(self.say('555', message='Hola').status_code, 404)
        # A validly signed session that points at a Telegram chat is still refused.
        self.assertEqual(self.say(web_chat.session_for(conversation), message='Hola').status_code, 404)

    def test_a_body_that_is_not_utf8_is_a_bad_request(self):
        body = '{"session": "x", "message": "¿Hola?"}'.encode('cp1252')
        self.assertEqual(self.client.post('/api/chat/messages/', body, content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post('/api/login/', body, content_type='application/json').status_code, 400)

    def test_long_and_empty_messages_are_rejected(self):
        session = self.start()['session']
        self.assertEqual(self.say(session, message='').status_code, 400)
        self.assertEqual(self.say(session, message='a' * 501).status_code, 400)

    @override_settings(WEB_CHAT_MAX_MESSAGES=2)
    def test_each_conversation_has_a_message_limit(self):
        self.use_model(text('Uno'), text('Dos'))
        session = self.start()['session']
        self.assertEqual(self.say(session, message='1').status_code, 200)
        self.assertEqual(self.say(session, message='2').status_code, 200)
        self.assertEqual(self.say(session, message='3').status_code, 429)

    @override_settings(WEB_CHAT_MAX_PER_IP_PER_HOUR=1)
    def test_each_visitor_has_an_hourly_limit(self):
        self.use_model(text('Uno'))
        self.assertEqual(self.say(self.start()['session'], message='1').status_code, 200)
        self.assertEqual(self.say(self.start()['session'], message='1').status_code, 429)

    def test_every_request_gets_its_own_client(self):
        # Under WSGI each async view runs in a new event loop: a shared client fails from the second request on.
        fakes = [FakeOpenAI(text('Uno')), FakeOpenAI(text('Dos'))]
        with mock.patch('chatbot.bot.ai.AsyncOpenAI', side_effect=fakes):
            session = self.start()['session']
            self.say(session, message='1')
            self.say(session, message='2')
        self.assertEqual([len(fake.requests) for fake in fakes], [1, 1])

    def test_a_model_failure_is_reported_and_nothing_is_lost(self):
        self.use_model()  # no scripted answers: the call raises
        session = self.start()['session']
        self.assertEqual(self.say(session, message='Hola').status_code, 502)
        self.assertEqual(len(self.client.get('/api/chat/state/', {'session': session}).json()['messages']), 1)

    def test_old_demo_data_is_deleted(self):
        self.use_model(tool_call('save_prospect_info', {'name': 'Ana López'}), text('Gracias'))
        self.say(self.start()['session'], message='Soy Ana')
        telegram = Prospect.objects.create(telegram_id='555', name='Cliente real')
        long_ago = timezone.now() - timedelta(hours=25)
        Conversation.objects.update(updated_at=long_ago)
        Prospect.objects.update(updated_at=long_ago)
        self.start()
        self.assertEqual(Conversation.objects.filter(channel=Channel.WEB).count(), 1)  # only the new one
        self.assertEqual(list(Prospect.objects.all()), [telegram])

    def test_web_chats_are_never_nudged_on_telegram(self):
        self.start()
        Conversation.objects.update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(_due_conversations(timezone.now()), [])

    @override_settings(WEB_CHAT_PROXY_HOPS=1)
    def test_behind_a_proxy_the_visitor_address_comes_from_the_header(self):
        request = mock.Mock(headers={'X-Forwarded-For': '6.6.6.6, 203.0.113.9'}, META={'REMOTE_ADDR': '10.0.0.1'})
        self.assertEqual(web_chat.client_ip(request), '203.0.113.9')
