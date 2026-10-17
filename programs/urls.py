from django.urls import path

from programs import views

urlpatterns = [
    path("umrah/<int:pk>/manifest/", views.umrah_manifest, name="umrah_manifest"),
    path("", views.programs_dashboard, name="programs_dashboard"),
    path("impact/", views.impact_report, name="program_impact"),
    path("<int:pk>/attendance/", views.attendance_sheet, name="program_attendance"),
    path("certificate/<int:pk>/", views.certificate, name="program_certificate"),
]
