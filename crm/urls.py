from django.urls import path

from crm import views

urlpatterns = [
    path("pipeline/", views.pipeline, name="crm_pipeline"),
    path("followups/", views.followups, name="crm_followups"),
]
