from django.urls import path

from governance import views

urlpatterns = [
    path("members/", views.members_dashboard, name="members_dashboard"),
    path("members/generate-fees/", views.generate_year_fees, name="generate_year_fees"),
    path("", views.governance_dashboard, name="governance_dashboard"),
    path("ncnp/load/", views.load_ncnp_framework, name="load_ncnp_framework"),
    path("readiness/", views.readiness_report, name="governance_readiness"),
    path("indicator/<int:pk>/evidence/", views.upload_evidence, name="upload_evidence"),
    path("evidence/<int:pk>/download/", views.download_evidence, name="download_evidence"),
    path("evidence/<int:pk>/delete/", views.delete_evidence, name="delete_evidence"),
]
