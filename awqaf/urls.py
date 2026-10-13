from django.urls import path

from awqaf import views

urlpatterns = [path("", views.awqaf_dashboard, name="awqaf_dashboard")]
