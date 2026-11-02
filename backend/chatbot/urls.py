from django.urls import path

from .views import (
    login_view,
    prospect_conversation_view,
    prospect_detail_view,
    prospect_id_document_view,
    prospects_view,
    settings_view,
)

urlpatterns = [
    path('api/login/', login_view, name='login'),
    path('api/settings/', settings_view, name='settings'),
    path('api/prospects/', prospects_view, name='prospects'),
    path('api/prospects/<int:prospect_id>/', prospect_detail_view, name='prospect_detail'),
    path('api/prospects/<int:prospect_id>/id-document/', prospect_id_document_view, name='prospect_id_document'),
    path('api/prospects/<int:prospect_id>/conversation/', prospect_conversation_view, name='prospect_conversation'),
]
