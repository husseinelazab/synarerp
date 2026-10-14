from django.urls import path

from store import views

urlpatterns = [
    path("", views.store_dashboard, name="store_dashboard"),
]
