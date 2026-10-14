from django.urls import path

from fleet import views

urlpatterns = [
    path("", views.fleet_dashboard, name="fleet_dashboard"),
]
