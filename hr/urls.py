from django.urls import path

from hr import views

urlpatterns = [
    path("settings/", views.payroll_settings, name="payroll_settings"),
    path("leave-balances/", views.leave_balances, name="leave_balances"),
    path("payroll/<int:pk>/payslips/", views.payslips, name="payslips"),
    path("payroll/<int:pk>/bank-file/", views.bank_file, name="payroll_bank_file"),
]
