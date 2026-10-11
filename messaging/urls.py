from django.urls import path

from messaging import views

urlpatterns = [
    path("settings/messaging/", views.settings_page, name="messaging_settings"),
    path("settings/messaging/log/", views.message_log, name="message_log"),
    path("messaging/send/<str:kind>/<int:pk>/", views.send_view, name="messaging_send"),
]
