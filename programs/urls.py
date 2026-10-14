from django.urls import path

from programs import views

urlpatterns = [
    path("", views.programs_dashboard, name="programs_dashboard"),
]
