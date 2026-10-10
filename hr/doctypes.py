from core.registry import ChildTable, DocType, register
from hr.models import Department, Designation, Employee, LeaveApplication, LeaveType, PayrollLine, PayrollRun

register(DocType(
    slug="employee", model=Employee, module="hr",
    list_fields=["employee_number", "full_name", "department", "designation", "status"],
    search_fields=["employee_number", "full_name", "national_id", "phone"],
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
        display_fields=["employee", "worked_days", "basic", "housing", "transport", "other_allowance", "additions",
                        "gross_pay", "unpaid_days", "unpaid_deduction", "gosi_employee", "other_deductions", "net_pay"],
        help="اترك الجدول فارغاً ليُجلب كل الموظفين على رأس العمل تلقائياً عند الحفظ. "
             "الرواتب والبدلات والتأمينات وخصم الإجازات بدون راتب تُحسب تلقائياً من ملف الموظف؛ "
             "أدخل هنا الإضافات والاستقطاعات اليدوية فقط.",
    ),
    totals=["total_earnings", "total_deductions", "total_gosi_employer", "total_net"],
    detail_template="hr/_payroll_detail.html",
))
