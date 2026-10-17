from django.urls import path

from programs import views, intake_views, referrals

urlpatterns = [
    path("referrals/<int:pk>/review/", referrals.review, name="health_referral_review"),
    path("specialty/", intake_views.dashboard, name="specialty_dashboard"),
    path("specialty/<int:pk>/review/", intake_views.review, name="specialty_review"),
    path("umrah/<int:pk>/manifest/", views.umrah_manifest, name="umrah_manifest"),
    path("", views.programs_dashboard, name="programs_dashboard"),
    path("impact/", views.impact_report, name="program_impact"),
    path("<int:pk>/attendance/", views.attendance_sheet, name="program_attendance"),
    path("certificate/<int:pk>/", views.certificate, name="program_certificate"),
]
