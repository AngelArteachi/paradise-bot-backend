import base64
import shutil
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from chatbot.bot import identity
from chatbot.bot.assistant import FALLBACK_REPLY, MAX_TOOL_ROUNDS, Assistant, trim_history
from chatbot.bot.config import BotConfig
from chatbot.bot.notify import advisor_summary
from chatbot.bot.tools import save_prospect_info
from chatbot.models import Prospect

from .fakes import FakeOpenAI, text, tool_call

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=ZoneInfo('America/Cancun'))

CONFIG = BotConfig(
    telegram_token='t', openai_api_key='k', model='gpt-4o-mini', vision_model='gpt-4o-mini', temperature=0.7,
    max_tokens=500, assistant_name='Marina', tone='Amigable', emojis_enabled=False, welcome_message='¡Hola!',
    system_instructions='', advisor_chat_id='-100', privacy_notice_url='https://paradisetour.example/aviso',
)

COMPLETE = {'name': 'Ana López', 'phone': '+52 998 123 4567', 'email': 'ana@example.com', 'destination': 'Cancún',
            'dates': '10 al 15 de marzo', 'nights': 5, 'passengers_adult': 2, 'passengers_child': 1}


class SaveProspectToolTest(TestCase):
    def test_saves_what_the_model_extracted_and_normalises_the_phone(self):
        result = save_prospect_info('42', {'name': 'Ana López', 'phone': '+52 998 123-4567'})
        self.assertIn('saved', result.message)
        prospect = Prospect.objects.get(telegram_id='42')
        self.assertEqual(prospect.phone, '+529981234567')

    def test_a_rejected_field_saves_nothing(self):
        save_prospect_info('42', {'name': 'Ana López'})
        result = save_prospect_info('42', {'email': 'ana@example.com', 'destination': 'Madrid'})
        self.assertIn('Validation error', result.message)
        self.assertIn('Caribe', result.message)
        prospect = Prospect.objects.get(telegram_id='42')
        self.assertEqual(prospect.email, '')  # the valid field in the same call was not written either

    def test_reports_completion_once(self):
        Prospect.objects.create(telegram_id='42', ine_file_id='tg-file')
        first = save_prospect_info('42', COMPLETE)
        again = save_prospect_info('42', {'nights': 6})
        self.assertIsNotNone(first.newly_complete)
        self.assertIsNone(again.newly_complete)

    def test_passenger_counts_must_be_numbers(self):
        result = save_prospect_info('42', {'passengers_adult': 'dos'})
        self.assertIn('whole number', result.message)


class AssistantTest(TestCase):
    async def test_answers_without_tools(self):
        client = FakeOpenAI(text('¡Claro! ¿A qué destino del Caribe te gustaría viajar?'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Quiero viajar', NOW)
        self.assertEqual(turn.reply, '¡Claro! ¿A qué destino del Caribe te gustaría viajar?')
        self.assertEqual([m['role'] for m in turn.messages], ['user', 'assistant'])

    async def test_saves_data_then_answers(self):
        client = FakeOpenAI(tool_call('save_prospect_info', {'name': 'Ana López'}), text('Gracias, Ana.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Me llamo Ana López', NOW)
        self.assertEqual(turn.reply, 'Gracias, Ana.')
        self.assertEqual(await Prospect.objects.filter(telegram_id='7', name='Ana López').acount(), 1)
        # The second request carries the tool result back to the model.
        roles = [m['role'] for m in client.requests[1]['messages']]
        self.assertEqual(roles, ['system', 'user', 'assistant', 'tool'])

    async def test_handles_several_tool_rounds(self):
        client = FakeOpenAI(
            tool_call('save_prospect_info', {'name': 'Ana López'}, 'a'),
            tool_call('save_prospect_info', {'email': 'ana@example.com'}, 'b'),
            text('Listo.'),
        )
        turn = await Assistant(client, CONFIG).respond('7', [], 'Ana López, ana@example.com', NOW)
        self.assertEqual(turn.reply, 'Listo.')
        self.assertEqual(len(client.requests), 3)

    async def test_stops_a_model_that_never_stops_calling_tools(self):
        client = FakeOpenAI(*[tool_call('save_prospect_info', {'name': 'Ana López'}, str(i)) for i in range(MAX_TOOL_ROUNDS)])
        turn = await Assistant(client, CONFIG).respond('7', [], 'Hola', NOW)
        self.assertEqual(turn.reply, FALLBACK_REPLY)

    async def test_marks_the_conversation_finished_and_reports_complete_prospects(self):
        await Prospect.objects.acreate(telegram_id='7', ine_file_id='tg-file')
        client = FakeOpenAI(tool_call('save_prospect_info', COMPLETE), text('Un asesor te contactará.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Todos mis datos', NOW)
        self.assertEqual(len(turn.completed_prospects), 1)
        self.assertFalse(turn.finished)

        client = FakeOpenAI(tool_call('mark_conversation_finished', {}), text('¡Buen viaje!'))
        turn = await Assistant(client, CONFIG).respond('7', turn.messages, 'No, gracias', NOW)
        self.assertTrue(turn.finished)

    async def test_system_prompt_carries_settings_and_office_hours(self):
        client = FakeOpenAI(text('Hola'))
        await Assistant(client, CONFIG).respond('7', [], 'Hola', NOW)
        system = client.requests[0]['messages'][0]['content']
        self.assertIn("Your name is 'Marina'", system)
        self.assertIn('Do NOT use ANY emojis', system)
        self.assertIn('https://paradisetour.example/aviso', system)
        self.assertIn('at the office right now', system)


class HistoryTest(TestCase):
    def test_trimming_never_starts_inside_a_tool_exchange(self):
        messages = []
        for i in range(20):
            messages += [
                {'role': 'user', 'content': f'u{i}'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': str(i)}]},
                {'role': 'tool', 'tool_call_id': str(i), 'content': 'ok'},
                {'role': 'assistant', 'content': f'a{i}'},
            ]
        trimmed = trim_history(messages, limit=30)
        self.assertEqual(trimmed[0]['role'], 'user')
        self.assertLessEqual(len(trimmed), 30)
        self.assertEqual(trimmed[-1]['content'], 'a19')


class IdentityTest(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    async def test_sends_the_image_inline_never_a_telegram_url(self):
        upload = identity.Upload('file-1', 'ine.jpg', b'\xff\xd8 fake jpeg', 'image/jpeg')
        client = FakeOpenAI(text('Nombre: ANA LOPEZ GARCIA. Fecha de nacimiento: 01/02/1990'))
        note = await identity.describe_for_model(client, 'gpt-4o-mini', upload, '')
        url = client.requests[0]['messages'][0]['content'][1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/jpeg;base64,'))
        self.assertEqual(base64.b64decode(url.split(',', 1)[1]), upload.content)
        self.assertNotIn('api.telegram.org', url)
        self.assertIn('ID/INE válida', note)

    async def test_rejects_an_image_that_is_not_an_id(self):
        upload = identity.Upload('file-1', 'gato.jpg', b'cat', 'image/jpeg')
        note = await identity.describe_for_model(FakeOpenAI(text('INVALID_IMAGE_REJECT')), 'm', upload, '')
        self.assertIn('NO válida', note)

    async def test_documents_are_not_sent_to_the_vision_model(self):
        client = FakeOpenAI()
        note = await identity.describe_for_model(client, 'm', identity.Upload('f', 'ine.pdf', b'%PDF', 'application/pdf'), '')
        self.assertEqual(client.requests, [])
        self.assertIn('documento', note)

    def test_a_new_photo_replaces_the_old_file_under_a_random_name(self):
        first = identity.store('9', identity.Upload('a', 'Ana Lopez INE.jpg', b'one', 'image/jpeg'))
        old_path = first.ine_file.path
        second = identity.store('9', identity.Upload('b', 'otra.jpg', b'two', 'image/jpeg'))
        self.assertNotIn('Ana', second.ine_file.name)
        self.assertEqual(second.ine_file_id, 'b')
        import os
        self.assertFalse(os.path.exists(old_path))


class AdvisorSummaryTest(TestCase):
    def test_lists_the_booking_without_forwarding_the_id(self):
        prospect = Prospect(telegram_id='1', ine_file_id='x', **{**COMPLETE, 'phone': '+529981234567', 'nights': '5'})
        summary = advisor_summary(prospect)
        self.assertIn('Ana López', summary)
        self.assertIn('2 adulto(s) y 1 menor(es)', summary)
        self.assertIn('Identificación: recibida', summary)
