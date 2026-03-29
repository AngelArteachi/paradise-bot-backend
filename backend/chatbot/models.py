from django.db import models
from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError

phone_regex = RegexValidator(regex=r'^\+?1?\d{9,15}$', message="Phone format must be valid and numerical digits only.")
email_regex = RegexValidator(regex=r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', message="Formato de correo electrónico inválido.")

class Prospect(models.Model):
    telegram_id = models.CharField(max_length=50, unique=True)
    language = models.CharField(max_length=10, default='es')
    name = models.CharField(max_length=200, blank=True, default="")
    phone = models.CharField(validators=[phone_regex], max_length=50, blank=True, default="")
    email = models.EmailField(validators=[email_regex], blank=True, default="")
    ine_file_id = models.CharField(max_length=200, blank=True, default="")
    dates = models.CharField(max_length=200, blank=True, default="")
    nights = models.CharField(max_length=50, blank=True, default="")
    passengers_adult = models.IntegerField(validators=[MinValueValidator(0), MaxValueValidator(30)], blank=True, null=True)
    passengers_child = models.IntegerField(validators=[MinValueValidator(0), MaxValueValidator(30)], blank=True, null=True, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} - {self.phone}"

class BotSetting(models.Model):
    # Telegram Settings
    bot_token = models.CharField(max_length=255, blank=True, default="", help_text="Unique token provided by @BotFather")
    assistant_name = models.CharField(max_length=255, blank=True, default="", help_text="Display name for your chatbot")
    
    # AI Configuration
    openai_api_key = models.CharField(max_length=255, blank=True, default="", help_text="Your OpenAI secret key")
    ai_model = models.CharField(max_length=100, default='gpt-3.5-turbo', help_text="The engine used for generation")
    temperature = models.FloatField(default=0.7, help_text="Randomness factor")
    
    # Bot Personality & Controls
    max_tokens = models.IntegerField(default=1000, help_text="Tokens per answer")
    conversation_tone = models.CharField(max_length=100, blank=True, default="", help_text="General personality style")
    emojis_enabled = models.BooleanField(default=True, help_text="Manage emojis")
    welcome_message = models.TextField(blank=True, default="", help_text="Sent immediately when a user starts the bot")
    system_instructions = models.TextField(blank=True, default="", help_text="Guidelines and persona traits the bot must follow")
    
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.assistant_name or "Global AI Configuration"

from django.contrib.auth.hashers import make_password, check_password

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
