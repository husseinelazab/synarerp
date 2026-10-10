from django.urls import path

from hr import views

urlpatterns = [
    path("settings/", views.payroll_settings, name="payroll_settings"),
    path("attendance/daily/", views.attendance_daily, name="attendance_daily"),
    path("attendance/monthly/", views.attendance_monthly, name="attendance_monthly"),
    path("eos-provision/", views.eos_provision, name="eos_provision"),
    path("leave-balances/", views.leave_balances, name="leave_balances"),
    path("payroll/<int:pk>/payslips/", views.payslips, name="payslips"),
    path("payroll/<int:pk>/bank-file/", views.bank_file, name="payroll_bank_file"),
]
