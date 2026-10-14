from django.urls import path

from programs import views

urlpatterns = [
    path("verify/<str:code>/", views.public_verify, name="program_verify"),
    path("<str:token>/", views.public_register, name="program_register"),
]
