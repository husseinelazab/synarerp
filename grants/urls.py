from django.urls import path

from grants import views

urlpatterns = [
    path("", views.grants_dashboard, name="grants_dashboard"),
    path("<int:pk>/financial-report/", views.grant_financial_report, name="grant_financial_report"),
]
