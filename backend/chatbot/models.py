import uuid
from pathlib import Path

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from .destinations import is_caribbean

phone_regex = RegexValidator(regex=r'^\+?1?\d{9,15}$', message="Phone format must be valid and numerical digits only.")
email_regex = RegexValidator(regex=r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', message="Formato de correo electrónico inválido.")


class Channel(models.TextChoices):
    TELEGRAM = 'telegram', 'Telegram'
    WEB = 'web', 'Chat web (demo)'


def id_document_path(instance, filename):
    """IDs are stored under a random name, so a file path never reveals or guesses a person."""
    suffix = Path(filename).suffix.lower()[:8] or '.jpg'
    return f'ines/{uuid.uuid4().hex}{suffix}'


class Prospect(models.Model):
    class Stage(models.TextChoices):
        NEW = 'new', 'Nuevo'
        CONTACTED = 'contacted', 'Contactado'
        QUOTED = 'quoted', 'Cotizado'
        WON = 'won', 'Reservó'
        LOST = 'lost', 'No reservó'

    # Telegram user id, or a `web-...` id for the web chat.
    telegram_id = models.CharField(max_length=50, unique=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.TELEGRAM, verbose_name="Canal")
    language = models.CharField(max_length=10, default='es')
    name = models.CharField(max_length=200, blank=True, default="")
    phone = models.CharField(validators=[phone_regex], max_length=50, blank=True, default="")
    email = models.EmailField(validators=[email_regex], blank=True, default="")
    ine_file_id = models.CharField(max_length=200, blank=True, default="")
    dates = models.CharField(max_length=200, blank=True, default="")
    nights = models.CharField(max_length=50, blank=True, default="")
    destination = models.CharField(max_length=255, blank=True, default="", verbose_name="Destino de interés")
    ine_file = models.FileField(upload_to=id_document_path, blank=True, null=True, verbose_name="Archivo de Identificación")
    passengers_adult = models.IntegerField(validators=[MinValueValidator(0), MaxValueValidator(30)], blank=True, null=True)
    passengers_child = models.IntegerField(validators=[MinValueValidator(0), MaxValueValidator(30)], blank=True, null=True, default=0)
    # 1 = visible, 0 = deleted from the panel (soft delete).
    status = models.IntegerField(default=1, validators=[MinValueValidator(0), MaxValueValidator(1)])
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW, verbose_name="Etapa")
    advisor_notes = models.TextField(blank=True, default="", verbose_name="Notas del asesor")
    advisor_notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.destination and not is_caribbean(self.destination):
            raise ValidationError({'destination': "Solo ofrecemos viajes a destinos del Caribe."})

    @property
    def has_id_document(self) -> bool:
        return bool(self.ine_file or self.ine_file_id)

    @property
    def is_complete(self) -> bool:
        """Everything an advisor needs to call the person back and quote the trip."""
        return bool(
            self.name and self.phone and self.email and self.has_id_document
            and self.destination and self.dates and self.nights and self.passengers_adult is not None
        )

    def __str__(self):
        return f"{self.name} - {self.phone}"


class Conversation(models.Model):
    """
    One chat with the assistant, on Telegram or on the web. The message
    history lives here, not in the bot's memory, so a restart or a redeploy
    does not make the assistant forget what a traveller already said.
    """

    Channel = Channel

    telegram_id = models.CharField(max_length=50, unique=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.TELEGRAM)
    # Telegram chat to write to; 0 for the web chat.
    chat_id = models.BigIntegerField()
    # OpenAI chat messages (user, assistant and tool turns); the system prompt is rebuilt on every turn.
    messages = models.JSONField(default=list, blank=True)
    is_finished = models.BooleanField(default=False)
    reengagement_attempts = models.IntegerField(default=0)
    last_user_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Conversación {self.telegram_id}"


class BotSetting(models.Model):
    user = models.OneToOneField('User', on_delete=models.CASCADE, related_name='bot_setting', null=True, blank=True)

    # Telegram Settings
    bot_token = models.CharField(max_length=255, blank=True, default="", help_text="Unique token provided by @BotFather")
    assistant_name = models.CharField(max_length=255, blank=True, default="", help_text="Display name for your chatbot")
    advisor_chat_id = models.CharField(max_length=50, blank=True, default="", help_text="Telegram chat or group that is notified of every complete prospect")

    # AI Configuration
    openai_api_key = models.CharField(max_length=255, blank=True, default="", help_text="Your OpenAI secret key")
    ai_model = models.CharField(max_length=100, default='gpt-4o-mini', help_text="The engine used for generation")
    temperature = models.FloatField(default=0.7, help_text="Randomness factor")

    # Bot Personality & Controls
    max_tokens = models.IntegerField(default=1000, help_text="Tokens per answer")
    conversation_tone = models.CharField(max_length=100, blank=True, default="", help_text="General personality style")
    emojis_enabled = models.BooleanField(default=True, help_text="Manage emojis")
    welcome_message = models.TextField(blank=True, default="", help_text="Sent immediately when a user starts the bot")
    system_instructions = models.TextField(blank=True, default="", help_text="Guidelines and persona traits the bot must follow")
    privacy_notice_url = models.URLField(blank=True, default="", help_text="Aviso de privacidad shared before asking for personal data")

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.assistant_name or "Global AI Configuration"


user_regex = RegexValidator(regex=r'^[a-zA-Z0-9_]+$', message="El usuario solo puede contener letras, números y guiones bajos.")


class User(models.Model):
    usuario = models.CharField(validators=[user_regex], max_length=150, unique=True)
    correo = models.EmailField(validators=[email_regex], unique=True)
    contrasena = models.CharField(max_length=128)

    def save(self, *args, **kwargs):
        # Si la contraseña está en texto plano, la validaremos y encriptaremos de forma irreversible y segura antes de guardarla a la BD
        if self.contrasena and not self.contrasena.startswith(('pbkdf2_sha256$', 'bcrypt$', 'argon2')):
            if len(self.contrasena) < 8:
                raise ValidationError("La contraseña debe tener al menos 8 caracteres para ser segura.")
            self.contrasena = make_password(self.contrasena)
        super().save(*args, **kwargs)

    def check_password(self, raw_password):
        """Verifica si la contraseña dada en texto plano coincide con el hash guardado de la BD"""
        return check_password(raw_password, self.contrasena)

    def __str__(self):
        return self.usuario
