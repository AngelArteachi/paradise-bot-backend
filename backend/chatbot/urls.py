from django.urls import path
from .views import login_view, settings_view

urlpatterns = [
    path('api/login/', login_view, name='login'),
    path('api/settings/', settings_view, name='settings'),
]
