from django.urls import path

from relations import views

urlpatterns = [
    path("", views.relations_dashboard, name="relations_dashboard"),
]
