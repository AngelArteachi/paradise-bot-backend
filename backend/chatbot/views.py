"""
JSON API for the advisors' panel. The routes and response fields are the ones
the panel already uses; additions are new fields and new routes only.
"""

import json
import logging

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms.models import model_to_dict
from django.http import FileResponse, Http404, JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from .models import BotSetting, Conversation, Prospect, User

log = logging.getLogger(__name__)

TOKEN_SALT = 'paradise.panel-session'
DOCUMENT_SALT = 'paradise.id-document'
DOCUMENT_LINK_MAX_AGE = 5 * 60

SECRET_FIELDS = ('openai_api_key', 'bot_token')
EDITABLE_SETTINGS = (
    'assistant_name', 'temperature', 'emojis_enabled', 'conversation_tone', 'welcome_message',
    'system_instructions', 'bot_token', 'openai_api_key', 'ai_model', 'max_tokens',
    'advisor_chat_id', 'privacy_notice_url',
)


def _json_body(request) -> dict:
    try:
        body = json.loads(request.body or b'{}')
    except ValueError as error:  # also a body that is not UTF-8
        raise ValueError('El cuerpo de la petición no es JSON válido.') from error
    if not isinstance(body, dict):
        raise ValueError('Se esperaba un objeto JSON.')
    return body


def get_user_from_token(request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    try:
        data = signing.loads(auth_header[7:], salt=TOKEN_SALT, max_age=settings.PANEL_TOKEN_MAX_AGE)
    except signing.BadSignature:
        # Covers expired tokens too (SignatureExpired is a BadSignature).
        return None
    return User.objects.filter(id=data.get("user_id")).first()


def _unauthorized():
    return JsonResponse({"error": "No autorizado"}, status=401)


@csrf_exempt
def login_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido"}, status=405)
    try:
        data = _json_body(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    user = User.objects.filter(usuario=data.get("usuario")).first()
    if user and user.check_password(data.get("contrasena") or ""):
        token = signing.dumps({'user_id': user.id}, salt=TOKEN_SALT)
        return JsonResponse({"message": "Login exitoso", "token": token, "expires_in": settings.PANEL_TOKEN_MAX_AGE}, status=200)
    return JsonResponse({"error": "Credenciales inválidas"}, status=401)


def mask_secret(value: str) -> str:
    """Shows enough to recognise a key, never enough to use it."""
    if not value:
        return ""
    return f"{value[:3]}…{value[-4:]}" if len(value) > 10 else "…"


def _settings_payload(setting: BotSetting) -> dict:
    data = model_to_dict(setting)
    data.pop('id', None)
    data.pop('user', None)
    for field in SECRET_FIELDS:
        data[f'{field}_set'] = bool(data[field])
        data[field] = mask_secret(data[field])
    return data


@csrf_exempt
def settings_view(request):
    user = get_user_from_token(request)
    if not user:
        return _unauthorized()

    setting, _ = BotSetting.objects.get_or_create(user=user)

    if request.method == "GET":
        return JsonResponse(_settings_payload(setting), status=200)

    if request.method in ("POST", "PUT"):
        try:
            body = _json_body(request)
        except ValueError as error:
            return JsonResponse({"error": str(error)}, status=400)
        for key in EDITABLE_SETTINGS:
            if key not in body:
                continue
            # The panel shows secrets masked; sending the mask back means "unchanged".
            if key in SECRET_FIELDS and '…' in str(body[key]):
                continue
            setattr(setting, key, body[key])
        try:
            setting.full_clean(exclude=['user'])
        except ValidationError as error:
            return JsonResponse({"error": "Datos inválidos", "fields": error.message_dict}, status=400)
        setting.save()
        return JsonResponse(_settings_payload(setting), status=200)

    return JsonResponse({"error": "Método no permitido"}, status=405)


def document_link(request, prospect: Prospect) -> str | None:
    """A link to the ID that works for five minutes, so the panel can show it in an <img> without the token."""
    if not prospect.ine_file:
        return None
    signature = signing.dumps({'p': prospect.pk}, salt=DOCUMENT_SALT)
    url = reverse('prospect_id_document', args=[prospect.pk])
    return request.build_absolute_uri(f"{url}?signature={signature}")


def _prospect_payload(request, prospect: Prospect) -> dict:
    data = model_to_dict(prospect)
    data.pop('ine_file', None)
    data['created_at'] = prospect.created_at.isoformat() if prospect.created_at else None
    data['updated_at'] = prospect.updated_at.isoformat() if prospect.updated_at else None
    data['advisor_notified_at'] = prospect.advisor_notified_at.isoformat() if prospect.advisor_notified_at else None
    data['has_id_document'] = prospect.has_id_document
    data['is_complete'] = prospect.is_complete
    data['ine_file_url'] = document_link(request, prospect)
    return data


@csrf_exempt
def prospects_view(request):
    user = get_user_from_token(request)
    if not user:
        return _unauthorized()
    if request.method != "GET":
        return JsonResponse({"error": "Método no permitido"}, status=405)

    prospects = Prospect.objects.filter(status=1).order_by("-created_at")
    stage = request.GET.get('stage')
    if stage:
        prospects = prospects.filter(stage=stage)
    return JsonResponse([_prospect_payload(request, p) for p in prospects], safe=False, status=200)


@csrf_exempt
def prospect_detail_view(request, prospect_id):
    user = get_user_from_token(request)
    if not user:
        return _unauthorized()
    prospect = Prospect.objects.filter(id=prospect_id, status=1).first()
    if prospect is None:
        return JsonResponse({"error": "Prospecto no encontrado"}, status=404)

    if request.method == "GET":
        return JsonResponse(_prospect_payload(request, prospect), status=200)

    if request.method == "PATCH":
        try:
            body = _json_body(request)
        except ValueError as error:
            return JsonResponse({"error": str(error)}, status=400)
        if 'stage' in body:
            if body['stage'] not in Prospect.Stage.values:
                return JsonResponse({"error": "Etapa no válida", "stages": Prospect.Stage.values}, status=400)
            prospect.stage = body['stage']
        if 'advisor_notes' in body:
            prospect.advisor_notes = str(body['advisor_notes'])[:5000]
        prospect.save(update_fields=['stage', 'advisor_notes', 'updated_at'])
        return JsonResponse(_prospect_payload(request, prospect), status=200)

    if request.method == "DELETE":
        prospect.status = 0
        prospect.save(update_fields=['status', 'updated_at'])
        return JsonResponse({"message": "Prospecto eliminado lógicamente"}, status=200)

    return JsonResponse({"error": "Método no permitido"}, status=405)


def prospect_id_document_view(request, prospect_id):
    """Serves an uploaded ID only with a valid, recent signature for that same prospect."""
    try:
        data = signing.loads(request.GET.get('signature', ''), salt=DOCUMENT_SALT, max_age=DOCUMENT_LINK_MAX_AGE)
    except signing.BadSignature:
        return JsonResponse({"error": "El enlace no es válido o ya expiró"}, status=403)
    if data.get('p') != prospect_id:
        return JsonResponse({"error": "El enlace no es válido o ya expiró"}, status=403)
    prospect = Prospect.objects.filter(id=prospect_id, status=1).first()
    if prospect is None or not prospect.ine_file:
        raise Http404
    response = FileResponse(prospect.ine_file.open('rb'))
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@csrf_exempt
def prospect_conversation_view(request, prospect_id):
    """The chat between the traveller and the assistant, so an advisor has context before calling."""
    user = get_user_from_token(request)
    if not user:
        return _unauthorized()
    prospect = Prospect.objects.filter(id=prospect_id, status=1).first()
    if prospect is None:
        return JsonResponse({"error": "Prospecto no encontrado"}, status=404)
    conversation = Conversation.objects.filter(telegram_id=prospect.telegram_id).first()
    messages = []
    for message in (conversation.messages if conversation else []):
        if message.get('role') in ('user', 'assistant') and message.get('content'):
            messages.append({'role': message['role'], 'content': message['content']})
    return JsonResponse({
        "prospect_id": prospect.id,
        "updated_at": conversation.updated_at.isoformat() if conversation else None,
        "messages": messages,
    }, status=200)
