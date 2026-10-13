from django.urls import path

from volunteering import views

urlpatterns = [
    path("verify/<str:code>/", views.public_verify, name="volunteer_verify"),
    path("<str:token>/", views.public_register, name="volunteer_register"),
]
