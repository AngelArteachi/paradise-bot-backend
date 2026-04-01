import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .models import User

@csrf_exempt
def login_view(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            usuario = data.get("usuario")
            contrasena = data.get("contrasena")
            
            user = User.objects.filter(usuario=usuario).first()
            if user and user.check_password(contrasena):
                from django.core.signing import dumps
                token = dumps({'user_id': user.id})
                return JsonResponse({"message": "Login exitoso", "token": token}, status=200)
            else:
                return JsonResponse({"error": "Credenciales inválidas"}, status=401)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"error": "Método no permitido"}, status=405)

from django.core.signing import loads
from django.forms.models import model_to_dict
from .models import BotSetting

def get_user_from_token(request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ")[1]
    try:
        data = loads(token)
        return User.objects.filter(id=data.get("user_id")).first()
    except Exception:
        return None

@csrf_exempt
def settings_view(request):
    user = get_user_from_token(request)
    if not user:
        return JsonResponse({"error": "No autorizado"}, status=401)
    
    settings, created = BotSetting.objects.get_or_create(user=user)
    
    if request.method == "GET":
        data = model_to_dict(settings)
        data.pop('id', None)
        data.pop('user', None)
        return JsonResponse(data, status=200)
    
    elif request.method == "POST" or request.method == "PUT":
        try:
            body = json.loads(request.body)
            allowed_fields = [
                "assistant_name", "temperature", "emojis_enabled", 
                "conversation_tone", "welcome_message", "system_instructions",
                "bot_token", "openai_api_key", "ai_model", "max_tokens"
            ]
            for key in allowed_fields:
                if key in body:
                    setattr(settings, key, body[key])
            settings.save()
            data = model_to_dict(settings)
            data.pop('id', None)
            data.pop('user', None)
            return JsonResponse(data, status=200)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
            
    return JsonResponse({"error": "Método no permitido"}, status=405)
