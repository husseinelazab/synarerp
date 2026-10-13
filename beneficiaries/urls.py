from django.urls import path

from beneficiaries import views

urlpatterns = [
    path("", views.beneficiaries_dashboard, name="beneficiaries_dashboard"),
    path("entities/", views.entities_dashboard, name="entities_dashboard"),
    path("aid/<int:pk>/send-otp/", views.aid_send_otp, name="aid_send_otp"),
    path("aid/<int:pk>/confirm/", views.aid_confirm, name="aid_confirm"),
]
