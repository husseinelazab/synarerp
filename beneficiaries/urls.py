from django.urls import path

from beneficiaries import views

urlpatterns = [
    path("", views.beneficiaries_dashboard, name="beneficiaries_dashboard"),
    path("entities/", views.entities_dashboard, name="entities_dashboard"),
]
