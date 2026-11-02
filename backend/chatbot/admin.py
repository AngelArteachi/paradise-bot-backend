from django.contrib import admin
from django.core import signing
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from chatbot.models import BotSetting, Conversation, Prospect, User as ChatbotUser
from chatbot.views import DOCUMENT_SALT


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'email', 'destination', 'dates', 'nights', 'passengers_adult', 'passengers_child', 'stage', 'has_ine', 'created_at')
    list_filter = ('stage', 'status', 'destination')
    search_fields = ('name', 'phone', 'email', 'destination')
    readonly_fields = ('ine_file_preview', 'created_at', 'updated_at', 'advisor_notified_at')
    exclude = ('ine_file',)

    @admin.display(boolean=True, description="ID Cargado")
    def has_ine(self, obj):
        return obj.has_id_document

    @admin.display(description="Archivo de ID")
    def ine_file_preview(self, obj):
        if not obj.ine_file:
            return "No cargado"
        signature = signing.dumps({'p': obj.pk}, salt=DOCUMENT_SALT)
        url = reverse('prospect_id_document', args=[obj.pk]) + f'?signature={signature}'
        return format_html('<a href="{}" target="_blank" rel="noopener">Ver identificación (enlace válido 5 minutos)</a>', url)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('telegram_id', 'is_finished', 'reengagement_attempts', 'last_user_message_at', 'updated_at')
    list_filter = ('is_finished',)
    search_fields = ('telegram_id',)
    readonly_fields = ('transcript',)
    exclude = ('messages',)

    @admin.display(description="Conversación")
    def transcript(self, obj):
        rows = [
            ('Viajero' if m['role'] == 'user' else 'Asistente', m['content'])
            for m in obj.messages if m.get('role') in ('user', 'assistant') and m.get('content')
        ]
        return format_html_join('', '<p><strong>{}:</strong> {}</p>', rows) or 'Sin mensajes'


@admin.register(BotSetting)
class BotSettingAdmin(admin.ModelAdmin):
    list_display = ('assistant_name', 'ai_model', 'temperature', 'updated_at')


@admin.register(ChatbotUser)
class ChatbotUserAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'correo')
    search_fields = ('usuario', 'correo')
