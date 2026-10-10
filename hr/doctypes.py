from core.registry import ChildTable, DocType, register
from hr.models import (Attendance, Department, Designation, Employee, EndOfServiceSettlement, LeaveApplication, LeaveType,
                       PayrollLine, PayrollRun)

register(DocType(
    slug="employee", model=Employee, module="hr",
    list_fields=["employee_number", "full_name", "department", "designation", "status"],
    search_fields=["employee_number", "full_name", "national_id", "phone"],
    detail_template="hr/_employee_detail.html",
))
register(DocType(
    slug="department", model=Department, module="hr", list_fields=["name", "parent"], search_fields=["name"],
))
register(DocType(
    slug="designation", model=Designation, module="hr", list_fields=["name"], search_fields=["name"],
))
register(DocType(
    slug="leave-type", model=LeaveType, module="hr", list_fields=["name", "days_per_year", "is_paid", "disabled"],
    search_fields=["name"],
))
register(DocType(
    slug="leave-application", model=LeaveApplication, module="hr",
    list_fields=["name", "employee", "leave_type", "from_date", "to_date", "total_days"],
    form_fields=["employee", "leave_type", "from_date", "to_date", "posting_date", "remarks"],
    search_fields=["name", "employee__full_name", "employee__employee_number"],
))
register(DocType(
    slug="payroll-run", model=PayrollRun, module="hr",
    list_fields=["name", "year", "month", "department", "total_net"],
    form_fields=["year", "month", "department", "posting_date", "remarks"],
    search_fields=["name"],
    child=ChildTable(
        PayrollLine, ["employee", "additions", "other_deductions", "notes"], label="رواتب الموظفين", min_rows=0,
        display_fields=["employee", "worked_days", "basic", "housing", "transport", "other_allowance", "overtime_pay",
                        "additions", "gross_pay", "unpaid_deduction", "absence_deduction", "late_deduction",
                        "gosi_employee", "other_deductions", "net_pay"],
        help="اترك الجدول فارغاً ليُجلب كل الموظفين على رأس العمل تلقائياً عند الحفظ. "
             "الرواتب والبدلات والتأمينات وخصم الإجازات بدون راتب تُحسب تلقائياً من ملف الموظف؛ "
             "أدخل هنا الإضافات والاستقطاعات اليدوية فقط.",
    ),
    totals=["total_earnings", "total_deductions", "total_gosi_employer", "total_eos_accrual", "total_net"],
    detail_template="hr/_payroll_detail.html",
))
register(DocType(
    slug="attendance", model=Attendance, module="hr",
    list_fields=["attendance_date", "employee", "status", "check_in", "check_out", "late_minutes", "overtime_hours"],
    search_fields=["employee__full_name", "employee__employee_number"],
))
register(DocType(
    slug="eos-settlement", model=EndOfServiceSettlement, module="hr",
    list_fields=["name", "employee", "last_working_date", "reason", "net_amount"],
    form_fields=["employee", "last_working_date", "reason", "leave_days_override", "additions", "other_deductions",
                 "posting_date", "remarks"],
    search_fields=["name", "employee__full_name", "employee__employee_number"],
    totals=["award", "leave_encashment", "additions", "other_deductions", "net_amount"],
    detail_template="hr/_eos_detail.html",
    detail_exclude=["service_years", "wage", "full_award", "entitlement_percent", "leave_days", "provision_balance",
                    "payment"],
))
