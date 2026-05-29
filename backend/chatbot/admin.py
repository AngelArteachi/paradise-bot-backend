from django.contrib import admin
from chatbot.models import Prospect, BotSetting, User as ChatbotUser

@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'email', 'dates', 'created_at')
    search_fields = ('name', 'phone', 'email')

@admin.register(BotSetting)
class BotSettingAdmin(admin.ModelAdmin):
    list_display = ('assistant_name', 'ai_model', 'temperature', 'updated_at')

@admin.register(ChatbotUser)
class ChatbotUserAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'correo')
    search_fields = ('usuario', 'correo')
