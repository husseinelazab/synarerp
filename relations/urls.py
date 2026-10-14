from django.urls import path

from relations import views

urlpatterns = [
    path("", views.relations_dashboard, name="relations_dashboard"),
    path("report/", views.relations_report, name="relations_report"),
    path("survey/<int:pk>/results/", views.survey_results, name="survey_results"),
]
