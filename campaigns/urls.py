from django.urls import path

from campaigns import views

urlpatterns = [
    path("", views.dashboard, name="campaigns_dashboard"),
    path("settings/", views.settings_page, name="campaigns_settings"),
    path("<int:pk>/test/", views.test_send, name="campaign_test_send"),
]
