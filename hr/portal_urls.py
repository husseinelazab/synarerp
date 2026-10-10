from django.urls import path

from hr import portal

urlpatterns = [
    path("", portal.portal_home, name="portal_home"),
    path("leave/", portal.portal_leave, name="portal_leave"),
    path("loan/", portal.portal_loan, name="portal_loan"),
    path("attendance/", portal.portal_attendance, name="portal_attendance"),
    path("payslip/<int:pk>/", portal.portal_payslip, name="portal_payslip"),
    path("withdraw/<str:kind>/<int:pk>/", portal.portal_withdraw, name="portal_withdraw"),
]
