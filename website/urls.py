from django.urls import path

from website import views

urlpatterns = [
    path("settings/", views.website_settings, name="website_settings"),
]
