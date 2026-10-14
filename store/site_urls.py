from django.urls import path

from store import site_views as views

urlpatterns = [
    path("", views.store_home, name="store_home"),
]
