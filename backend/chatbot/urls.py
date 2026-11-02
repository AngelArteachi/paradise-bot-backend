from django.urls import path

from .views import (
    login_view,
    prospect_conversation_view,
    prospect_detail_view,
    prospect_id_document_view,
    prospects_view,
    settings_view,
)
from .web_chat import chat_message_view, chat_start_view, chat_state_view, demo_page

urlpatterns = [
    path('', demo_page, name='demo'),
    path('api/chat/', chat_start_view, name='chat_start'),
    path('api/chat/state/', chat_state_view, name='chat_state'),
    path('api/chat/messages/', chat_message_view, name='chat_message'),
    path('api/login/', login_view, name='login'),
    path('api/settings/', settings_view, name='settings'),
    path('api/prospects/', prospects_view, name='prospects'),
    path('api/prospects/<int:prospect_id>/', prospect_detail_view, name='prospect_detail'),
    path('api/prospects/<int:prospect_id>/id-document/', prospect_id_document_view, name='prospect_id_document'),
    path('api/prospects/<int:prospect_id>/conversation/', prospect_conversation_view, name='prospect_conversation'),
]
