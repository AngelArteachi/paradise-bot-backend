from django.contrib import admin
from chatbot.models import Prospect, BotSetting, User as ChatbotUser

from django.utils.html import format_html

@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'email', 'destination', 'dates', 'nights', 'passengers_adult', 'passengers_child', 'has_ine', 'created_at')
    search_fields = ('name', 'phone', 'email', 'destination')
    readonly_fields = ('ine_file_preview', 'created_at')
    
    def has_ine(self, obj):
        return bool(obj.ine_file)
    has_ine.boolean = True
    has_ine.short_description = "ID Cargado"
    
    def ine_file_preview(self, obj):
        if obj.ine_file:
            return format_html('<a href="{0}" target="_blank">Ver Identificación ({1})</a>', obj.ine_file.url, obj.ine_file.name)
        return "No cargado"
    ine_file_preview.short_description = "Archivo de ID"

@admin.register(BotSetting)
class BotSettingAdmin(admin.ModelAdmin):
    list_display = ('assistant_name', 'ai_model', 'temperature', 'updated_at')

@admin.register(ChatbotUser)
class ChatbotUserAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'correo')
    search_fields = ('usuario', 'correo')
