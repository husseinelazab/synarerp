from django.urls import path

from manufacturing import views

urlpatterns = [
    path("mrp/", views.mrp, name="mrp"),
    path("report/", views.production_report, name="production_report"),
]
