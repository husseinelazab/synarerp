from django.urls import path

from beneficiaries import views, profile

urlpatterns = [
    path("<int:pk>/profile/", profile.unified, name="beneficiary_unified"),
    path("", views.beneficiaries_dashboard, name="beneficiaries_dashboard"),
    path("entities/", views.entities_dashboard, name="entities_dashboard"),
    path("aid/<int:pk>/send-otp/", views.aid_send_otp, name="aid_send_otp"),
    path("aid/<int:pk>/confirm/", views.aid_confirm, name="aid_confirm"),
    path("committee/", views.committee_page, name="committee_page"),
    path("committee/refer/<slug:slug>/<int:pk>/", views.committee_refer, name="committee_refer"),
    path("committee/case/<int:pk>/vote/", views.committee_vote, name="committee_vote"),
    path("committee/case/<int:pk>/minutes/", views.committee_minutes, name="committee_minutes"),
    path("vouchers/<int:pk>/unlock/", views.voucher_unlock, name="voucher_unlock"),
    path("vouchers/<int:pk>/resend/", views.voucher_resend, name="voucher_resend"),
    path("merchants/<int:pk>/regenerate/", views.merchant_regenerate, name="merchant_regenerate"),
]
