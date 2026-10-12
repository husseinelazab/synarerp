from django.urls import path

from projects import views

urlpatterns = [path("report/", views.project_report, name="project_report")]
