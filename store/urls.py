from django.urls import path

from store import views

urlpatterns = [
    path("", views.store_dashboard, name="store_dashboard"),
    path("settings/", views.store_settings, name="store_settings"),
    path("opportunity/<int:pk>/image/", views.opportunity_image, name="store_opportunity_image"),
    path("webhook/", views.webhook, name="store_webhook"),
]
