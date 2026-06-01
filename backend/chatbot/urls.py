from django.urls import path
from .views import login_view, settings_view, prospects_view, prospect_detail_view

urlpatterns = [
    path('api/login/', login_view, name='login'),
    path('api/settings/', settings_view, name='settings'),
    path('api/prospects/', prospects_view, name='prospects'),
    path('api/prospects/<int:prospect_id>/', prospect_detail_view, name='prospect_detail'),
]
