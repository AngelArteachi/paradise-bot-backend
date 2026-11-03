import base64
import shutil
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from chatbot.bot import identity
from chatbot.bot.assistant import FALLBACK_REPLY, MAX_TOOL_ROUNDS, SAVE_REMINDER, Assistant, trim_history
from chatbot.bot.config import BotConfig
from chatbot.bot.notify import advisor_summary
from chatbot.bot.tools import save_prospect_info
from chatbot.models import Prospect

from .fakes import FakeOpenAI, text, tool_call

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=ZoneInfo('America/Cancun'))

CONFIG = BotConfig(
    telegram_token='t', ai_api_key='k', ai_base_url='', model='gpt-4o-mini', vision_model='gpt-4o-mini', temperature=0.7,
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

    def test_tells_the_model_what_is_still_missing(self):
        # From a real chat: with only "saved" to go on, the model told the user an advisor would call, without dates.
        Prospect.objects.create(telegram_id='42', ine_file_id='demo-sample')
        result = save_prospect_info('42', {**COMPLETE, 'dates': '', 'nights': ''})
        self.assertIn('Still missing: travel dates.', result.message)
        self.assertIn('Do NOT say an advisor will contact them', result.message)
        self.assertIsNone(result.newly_complete)

    def test_exact_dates_give_the_nights(self):
        Prospect.objects.create(telegram_id='42', ine_file_id='demo-sample')
        arrival = timezone.localdate() + timedelta(days=30)
        stay = {'check_in': arrival.isoformat(), 'check_out': (arrival + timedelta(days=5)).isoformat()}
        # The nights sent alongside are a guess; the dates decide.
        result = save_prospect_info('42', {**COMPLETE, 'dates': '', 'nights': 6, **stay})
        prospect = Prospect.objects.get(telegram_id='42')
        self.assertEqual(prospect.nights, '5')
        self.assertEqual(prospect.dates, f"{arrival:%d/%m/%Y} al {arrival + timedelta(days=5):%d/%m/%Y}")
        self.assertIn('serían 5 noches', result.message)
        self.assertIsNotNone(result.newly_complete)

    def test_approximate_dates_leave_the_nights_to_ask(self):
        save_prospect_info('42', {'dates': 'Semana Santa'})
        result = save_prospect_info('42', {'name': 'Ana López'})
        self.assertIn('number of nights', result.message)
        self.assertNotIn('travel dates', result.message)

    def test_impossible_stays_save_nothing(self):
        today = timezone.localdate()
        for check_in, check_out in [(today - timedelta(days=1), today + timedelta(days=3)),  # a year the model got wrong
                                    (today + timedelta(days=9), today + timedelta(days=4)),
                                    (today, today + timedelta(days=61))]:
            result = save_prospect_info('42', {'name': 'Ana López', 'check_in': check_in.isoformat(), 'check_out': check_out.isoformat()})
            self.assertIn('Validation error', result.message)
        self.assertIn('YYYY-MM-DD', save_prospect_info('42', {'check_in': '20 de diciembre', 'check_out': '25 de diciembre'}).message)
        self.assertFalse(Prospect.objects.filter(telegram_id='42', name='Ana López').exists())

    def test_only_numbers_the_user_said_are_saved(self):
        # From real chats: "2 adultos 2 niños" came back with nights 7, "en Semana Santa" with made-up exact days.
        result = save_prospect_info('42', {'passengers_adult': 2, 'nights': 7}, said='2 adultos 2 niños.')
        self.assertIn('never said 7 nights', result.message)
        prospect = Prospect.objects.get(telegram_id='42')
        self.assertEqual((prospect.passengers_adult, prospect.nights), (2, ''))
        arrival = timezone.localdate() + timedelta(days=120)
        invented = {'dates': 'Semana Santa', 'check_in': arrival.isoformat(), 'check_out': (arrival + timedelta(days=7)).isoformat()}
        save_prospect_info('42', invented, said='Todavía no sé bien, en Semana Santa')
        self.assertEqual(Prospect.objects.get(telegram_id='42').dates, 'Semana Santa')

    def test_numbers_in_words_and_confirmed_proposals_count(self):
        save_prospect_info('42', {'nights': 4}, said='Unas cuatro noches')
        self.assertEqual(Prospect.objects.get(telegram_id='42').nights, '4')
        save_prospect_info('42', {'nights': 7}, said='Una semana\nSí\nEntonces serían 7 noches, ¿correcto?')
        self.assertEqual(Prospect.objects.get(telegram_id='42').nights, '7')

    def test_the_users_words_survive_exact_dates_the_model_got_wrong(self):
        # From a real chat: "en Semana Santa" became check_in 2026-03-29, months in the past.
        last_year = timezone.localdate() - timedelta(days=200)
        result = save_prospect_info('42', {'dates': 'Semana Santa', 'check_in': last_year.isoformat(),
                                           'check_out': (last_year + timedelta(days=2)).isoformat()})
        self.assertIn('saved as the user said them', result.message)
        self.assertEqual(Prospect.objects.get(telegram_id='42').dates, 'Semana Santa')

    def test_passenger_counts_must_be_numbers(self):
        result = save_prospect_info('42', {'passengers_adult': 'dos'})
        self.assertIn('whole number', result.message)

    def test_nights_must_be_a_number(self):
        save_prospect_info('42', {'nights': '4 noches'})
        self.assertEqual(Prospect.objects.get(telegram_id='42').nights, '4')
        # From a real chat: the model put the dates in the nights field.
        result = save_prospect_info('42', {'nights': 'hacia Semana Santa'})
        self.assertIn("'nights' must be a number", result.message)
        self.assertEqual(Prospect.objects.get(telegram_id='42').nights, '4')

    def test_zero_placeholders_are_not_answers(self):
        save_prospect_info('42', {'name': 'Ana López', 'nights': 0, 'passengers_adult': 0, 'passengers_child': 0})
        prospect = Prospect.objects.get(telegram_id='42')
        self.assertEqual((prospect.nights, prospect.passengers_adult, prospect.passengers_child), ('', None, 0))


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

    async def test_a_tool_call_written_as_text_is_still_run(self):
        written = text('Perfecto, Ana.\n\n{"name": "save_prospect_info", "arguments": {"name": "Ana López"}}')
        client = FakeOpenAI(written, text('Gracias, Ana.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Me llamo Ana López', NOW)
        self.assertEqual(turn.reply, 'Gracias, Ana.')
        self.assertEqual(await Prospect.objects.filter(telegram_id='7', name='Ana López').acount(), 1)
        self.assertEqual([m['role'] for m in client.requests[1]['messages']], ['system', 'user', 'assistant', 'tool'])

    async def test_a_model_that_skips_saving_contact_data_is_reminded_once(self):
        client = FakeOpenAI(text('¡Listo, lo guardé!'), tool_call('save_prospect_info', {'email': 'ana@example.com'}), text('Gracias.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Mi correo es ana@example.com', NOW)
        self.assertEqual(turn.reply, 'Gracias.')
        self.assertEqual(await Prospect.objects.filter(telegram_id='7', email='ana@example.com').acount(), 1)
        self.assertEqual(client.requests[1]['messages'][-1]['content'], SAVE_REMINDER)
        # The unsaved draft and the reminder stay out of the history.
        self.assertNotIn('¡Listo, lo guardé!', [m.get('content') for m in turn.messages])

    async def test_the_reminder_is_sent_at_most_once(self):
        client = FakeOpenAI(text('Anotado.'), text('De acuerdo.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Mi teléfono es 998 123 4567', NOW)
        self.assertEqual(turn.reply, 'De acuerdo.')
        self.assertEqual(len(client.requests), 2)

    async def test_trip_details_also_get_the_reminder(self):
        for message in ('2 adultos 2 niños.', 'Del 20 al 25 de diciembre', 'Quiero ir a Cozumel'):
            client = FakeOpenAI(text('¡Perfecto!'), text('¡Perfecto!'))
            await Assistant(client, CONFIG).respond('7', [], message, NOW)
            self.assertEqual(client.requests[1]['messages'][-1]['content'], SAVE_REMINDER, message)

    async def test_an_unfinished_profile_is_not_handed_to_an_advisor(self):
        await Prospect.objects.acreate(telegram_id='7', name='Ana López', ine_file_id='tg-file')
        client = FakeOpenAI(text('Un asesor se pondrá en contacto contigo pronto.'), text('¿A qué destino te gustaría viajar?'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Sí, correcto', NOW)
        self.assertEqual(turn.reply, '¿A qué destino te gustaría viajar?')
        self.assertIn('Still missing: phone, email, destination', client.requests[1]['messages'][-1]['content'])

    async def test_a_complete_profile_is_handed_over(self):
        await Prospect.objects.acreate(telegram_id='7', ine_file_id='tg-file', **{**COMPLETE, 'phone': '+529981234567'})
        client = FakeOpenAI(text('Un asesor se pondrá en contacto contigo pronto.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Gracias', NOW)
        self.assertEqual(turn.reply, 'Un asesor se pondrá en contacto contigo pronto.')

    async def test_json_that_is_not_a_tool_call_is_left_alone(self):
        client = FakeOpenAI(text('Un ejemplo: {"name": "Ana"}'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Hola', NOW)
        self.assertEqual(turn.reply, 'Un ejemplo: {"name": "Ana"}')

    async def test_handles_several_tool_rounds(self):
        client = FakeOpenAI(
            tool_call('save_prospect_info', {'name': 'Ana López'}, 'a'),
            tool_call('save_prospect_info', {'email': 'ana@example.com'}, 'b'),
            text('Listo.'),
        )
        turn = await Assistant(client, CONFIG).respond('7', [], 'Ana López, ana@example.com', NOW)
        self.assertEqual(turn.reply, 'Listo.')
        self.assertEqual(len(client.requests), 3)

    async def test_the_tool_checks_numbers_against_the_conversation(self):
        history = [{'role': 'user', 'content': 'Quiero 5 noches'}, {'role': 'assistant', 'content': '¿Cuántos adultos?'}]
        client = FakeOpenAI(tool_call('save_prospect_info', {'nights': 5, 'passengers_adult': 3}), text('Gracias.'))
        await Assistant(client, CONFIG).respond('7', history, 'Somos 2 adultos', NOW)
        prospect = await Prospect.objects.aget(telegram_id='7')
        self.assertEqual(prospect.nights, '5')  # said in an earlier message

    async def test_a_call_repeated_in_one_response_runs_once(self):
        repeated = tool_call('save_prospect_info', {'name': 'Ana López'})
        repeated.tool_calls *= 6
        client = FakeOpenAI(repeated, text('Gracias, Ana.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Me llamo Ana López', NOW)
        self.assertEqual(len(turn.tools), 1)
        self.assertEqual([m['role'] for m in client.requests[1]['messages']], ['system', 'user', 'assistant', 'tool'])

    async def test_stops_a_model_that_never_stops_calling_tools(self):
        client = FakeOpenAI(*[tool_call('save_prospect_info', {'name': 'Ana López'}, str(i)) for i in range(MAX_TOOL_ROUNDS)])
        turn = await Assistant(client, CONFIG).respond('7', [], 'Hola', NOW)
        self.assertEqual(turn.reply, FALLBACK_REPLY)

    async def test_marks_the_conversation_finished_and_reports_complete_prospects(self):
        await Prospect.objects.acreate(telegram_id='7', ine_file_id='tg-file')
        client = FakeOpenAI(tool_call('save_prospect_info', COMPLETE), text('Un asesor te contactará.'))
        turn = await Assistant(client, CONFIG).respond('7', [], 'Todos mis datos, 5 noches', NOW)
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
