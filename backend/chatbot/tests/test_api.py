import json
import shutil
import tempfile

from django.core import signing
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from chatbot.models import BotSetting, Conversation, Prospect, User
from chatbot.views import DOCUMENT_SALT


class PanelApiTest(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        User.objects.create(usuario='asesora', correo='asesora@paradise.example', contrasena='contraseña-segura')

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def login(self, password='contraseña-segura'):
        return self.client.post('/api/login/', json.dumps({'usuario': 'asesora', 'contrasena': password}), content_type='application/json')

    def auth(self):
        return {'HTTP_AUTHORIZATION': f"Bearer {self.login().json()['token']}"}

    def prospect_with_id(self, **fields):
        prospect = Prospect.objects.create(telegram_id=fields.pop('telegram_id', '1'), name='Ana', destination='Cancún', **fields)
        prospect.ine_file.save('ine.jpg', ContentFile(b'jpeg bytes'), save=True)
        return prospect

    # --- Sessions -----------------------------------------------------------

    def test_login_accepts_the_right_password_only(self):
        self.assertEqual(self.login('incorrecta').status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.json())

    def test_rejects_missing_forged_and_expired_tokens(self):
        self.assertEqual(self.client.get('/api/prospects/').status_code, 401)
        forged = signing.dumps({'user_id': 1})  # signed, but not as a panel session
        self.assertEqual(self.client.get('/api/prospects/', HTTP_AUTHORIZATION=f'Bearer {forged}').status_code, 401)
        headers = self.auth()
        with override_settings(PANEL_TOKEN_MAX_AGE=-1):
            self.assertEqual(self.client.get('/api/prospects/', **headers).status_code, 401)

    # --- Settings -----------------------------------------------------------

    def test_settings_never_return_secrets_in_clear(self):
        headers = self.auth()
        self.client.put('/api/settings/', json.dumps({'openai_api_key': 'sk-proj-abcdefghijklmnop1234', 'bot_token': '123456:ABCDEFGHIJ'}),
                        content_type='application/json', **headers)
        data = self.client.get('/api/settings/', **headers).json()
        self.assertEqual(data['openai_api_key'], 'sk-…1234')
        self.assertTrue(data['openai_api_key_set'])
        self.assertNotIn('ABCDEFGHIJ', json.dumps(data))

    def test_sending_the_mask_back_keeps_the_stored_secret(self):
        headers = self.auth()
        self.client.put('/api/settings/', json.dumps({'openai_api_key': 'sk-proj-abcdefghijklmnop1234'}), content_type='application/json', **headers)
        self.client.put('/api/settings/', json.dumps({'openai_api_key': 'sk-…1234', 'assistant_name': 'Marina'}),
                        content_type='application/json', **headers)
        setting = BotSetting.objects.get()
        self.assertEqual(setting.openai_api_key, 'sk-proj-abcdefghijklmnop1234')
        self.assertEqual(setting.assistant_name, 'Marina')

    def test_settings_are_validated(self):
        response = self.client.put('/api/settings/', json.dumps({'privacy_notice_url': 'no es una url'}),
                                   content_type='application/json', **self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertIn('privacy_notice_url', response.json()['fields'])

    # --- Prospects and IDs --------------------------------------------------

    def test_lists_prospects_with_a_temporary_link_to_the_id(self):
        prospect = self.prospect_with_id()
        listed = self.client.get('/api/prospects/', **self.auth()).json()[0]
        self.assertTrue(listed['has_id_document'])
        self.assertIn(f'/api/prospects/{prospect.id}/id-document/?signature=', listed['ine_file_url'])
        self.assertNotIn('/media/', listed['ine_file_url'])

        document = self.client.get(listed['ine_file_url'])
        self.assertEqual(document.status_code, 200)
        self.assertEqual(b''.join(document.streaming_content), b'jpeg bytes')
        self.assertEqual(document['Cache-Control'], 'private, no-store')

    def test_ids_need_a_valid_signature_for_that_same_prospect(self):
        ana = self.prospect_with_id(telegram_id='1')
        luis = self.prospect_with_id(telegram_id='2')
        self.assertEqual(self.client.get(f'/api/prospects/{ana.id}/id-document/').status_code, 403)
        for_luis = signing.dumps({'p': luis.id}, salt=DOCUMENT_SALT)
        self.assertEqual(self.client.get(f'/api/prospects/{ana.id}/id-document/?signature={for_luis}').status_code, 403)

    def test_ids_are_not_reachable_through_media_urls(self):
        prospect = self.prospect_with_id()
        self.assertEqual(self.client.get(f'/media/{prospect.ine_file.name}').status_code, 404)

    def test_advisors_move_prospects_through_the_pipeline(self):
        prospect = Prospect.objects.create(telegram_id='1', name='Ana')
        headers = self.auth()
        response = self.client.patch(f'/api/prospects/{prospect.id}/', json.dumps({'stage': 'quoted', 'advisor_notes': 'Prefiere todo incluido'}),
                                     content_type='application/json', **headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/prospects/?stage=quoted', **headers).json()[0]['advisor_notes'], 'Prefiere todo incluido')
        bad = self.client.patch(f'/api/prospects/{prospect.id}/', json.dumps({'stage': 'vendido'}), content_type='application/json', **headers)
        self.assertEqual(bad.status_code, 400)

    def test_delete_hides_the_prospect_without_erasing_it(self):
        prospect = Prospect.objects.create(telegram_id='1', name='Ana')
        headers = self.auth()
        self.assertEqual(self.client.delete(f'/api/prospects/{prospect.id}/', **headers).status_code, 200)
        self.assertEqual(self.client.get('/api/prospects/', **headers).json(), [])
        self.assertTrue(Prospect.objects.filter(id=prospect.id, status=0).exists())

    def test_conversation_shows_only_what_was_said(self):
        prospect = Prospect.objects.create(telegram_id='1', name='Ana')
        Conversation.objects.create(telegram_id='1', chat_id=1, messages=[
            {'role': 'user', 'content': 'Quiero ir a Cancún'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'x'}]},
            {'role': 'tool', 'tool_call_id': 'x', 'content': 'saved'},
            {'role': 'assistant', 'content': '¡Excelente elección!'},
        ])
        data = self.client.get(f'/api/prospects/{prospect.id}/conversation/', **self.auth()).json()
        self.assertEqual(data['messages'], [
            {'role': 'user', 'content': 'Quiero ir a Cancún'},
            {'role': 'assistant', 'content': '¡Excelente elección!'},
        ])
