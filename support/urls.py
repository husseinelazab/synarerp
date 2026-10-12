from django.urls import path

from support import views

urlpatterns = [path("", views.support_dashboard, name="support_dashboard")]
