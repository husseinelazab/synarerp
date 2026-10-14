from django.urls import path

from fleet import views

urlpatterns = [
    path("", views.fleet_dashboard, name="fleet_dashboard"),
    path("report/", views.fleet_report, name="fleet_report"),
    path("trip/<int:pk>/complete/", views.complete_trip, name="fleet_complete_trip"),
]
