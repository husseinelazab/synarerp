from core.registry import ChildTable, DocType, register
from hr.models import (Appraisal, AppraisalCriterion, AppraisalScore, AppraisalTemplate, Attendance, Department, Designation, Employee, EmployeeLoan, EndOfServiceSettlement, Holiday,
                       LeaveApplication, LeaveType, PayrollLine, PayrollRun, ShiftAssignment, WorkShift)

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
    list_fields=["name", "employee", "leave_type", "from_date", "to_date", "total_days", "requested_via_portal"],
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
                        "gosi_employee", "loan_deduction", "other_deductions", "net_pay"],
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
    totals=["award", "leave_encashment", "additions", "other_deductions", "loan_deduction", "net_amount"],
    detail_template="hr/_eos_detail.html",
    detail_exclude=["service_years", "wage", "full_award", "entitlement_percent", "leave_days", "provision_balance",
                    "payment"],
))
register(DocType(
    slug="employee-loan", model=EmployeeLoan, module="hr",
    list_fields=["name", "employee", "amount", "installment_amount", "repaid_amount"],
    form_fields=["employee", "amount", "installment_amount", "start_year", "start_month", "payment_account",
                 "posting_date", "remarks"],
    search_fields=["name", "employee__full_name", "employee__employee_number"],
    totals=["amount", "repaid_amount"],
    detail_template="hr/_loan_detail.html",
))
register(DocType(
    slug="holiday", model=Holiday, module="hr", list_fields=["name", "from_date", "to_date"], search_fields=["name"],
    list_actions=[("إضافة العطل الرسمية لهذه السنة (مع العيدين)", "add_fixed_holidays")],
    detail_template="hr/_holiday_detail.html",
))
register(DocType(
    slug="work-shift", model=WorkShift, module="hr",
    list_fields=["name", "start_time", "end_time", "grace_minutes", "hours_per_day"], search_fields=["name"],
))
register(DocType(
    slug="shift-assignment", model=ShiftAssignment, module="hr",
    list_fields=["employee", "shift", "from_date", "to_date"], search_fields=["employee__full_name", "shift__name"],
))
register(DocType(
    slug="appraisal-template", model=AppraisalTemplate, module="hr", list_fields=["name"], search_fields=["name"],
    child=ChildTable(AppraisalCriterion, ["name", "weight"], label="المعايير", help="مجموع الأوزان يجب أن يساوي 100."),
))
register(DocType(
    slug="appraisal", model=Appraisal, module="hr",
    list_fields=["name", "employee", "period_from", "period_to", "score_percent", "rating"],
    form_fields=["employee", "template", "period_from", "period_to", "strengths", "improvements", "posting_date",
                 "remarks"],
    search_fields=["name", "employee__full_name", "employee__employee_number"],
    child=ChildTable(AppraisalScore, ["criterion", "weight", "score", "comment"], label="المعايير والدرجات", min_rows=0,
                     help="اترك الجدول فارغاً لتُجلب معايير النموذج عند الحفظ، ثم أدخل درجة من 1 (ضعيف) إلى 5 (ممتاز) لكل معيار."),
))

from hr.models import AssociationRole, OrganizationUnit
from hr.organization_forms import AssociationRoleForm
register(DocType(slug="association-role", model=AssociationRole, module="office", form_class=AssociationRoleForm,
    list_fields=["name", "can_submit", "can_delete", "can_export"], search_fields=["name"]))
register(DocType(slug="organization-unit", model=OrganizationUnit, module="office",
    list_fields=["name", "kind", "parent", "responsible_name", "job_title", "role_policy"], search_fields=["name", "responsible_name"]))
