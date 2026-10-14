from django.urls import path

from excellence import views

urlpatterns = [
    path("", views.excellence_dashboard, name="excellence_dashboard"),
    path("kpi-entry/", views.kpi_entry, name="kpi_entry"),
    path("report/", views.performance_report, name="performance_report"),
]
