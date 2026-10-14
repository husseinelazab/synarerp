from django.urls import path

from excellence import views

urlpatterns = [
    path("", views.excellence_dashboard, name="excellence_dashboard"),
]
