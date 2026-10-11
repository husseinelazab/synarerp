from django.urls import path

from hr import views, wps

urlpatterns = [
    path("settings/", views.payroll_settings, name="payroll_settings"),
    path("settings/portal/<int:pk>/", views.portal_access, name="portal_access"),
    path("settings/devices/", views.attendance_devices, name="attendance_devices"),
    path("attendance/raw/", views.raw_punches, name="raw_punches"),
    path("dashboard/", views.hr_dashboard, name="hr_dashboard"),
    path("attendance/daily/", views.attendance_daily, name="attendance_daily"),
    path("attendance/monthly/", views.attendance_monthly, name="attendance_monthly"),
    path("attendance/punches/", views.attendance_punches, name="attendance_punches"),
    path("holidays/add-fixed/", views.add_fixed_holidays, name="add_fixed_holidays"),
    path("eos-provision/", views.eos_provision, name="eos_provision"),
    path("leave-balances/", views.leave_balances, name="leave_balances"),
    path("payroll/<int:pk>/payslips/", views.payslips, name="payslips"),
    path("payroll/<int:pk>/bank-file/", views.bank_file, name="payroll_bank_file"),
    path("payroll/<int:pk>/wps-file/", wps.wps_file, name="payroll_wps_file"),
]
