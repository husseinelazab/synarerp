"""ملف حماية الأجور (WPS) لرفعه في منصة مُدد أو بنك المنشأة: بيانات كل موظف وراتبه ومكوناته بعد اعتماد المسير."""
import csv
import io
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect

from core.models import DocStatus
from core.utils import csv_safe

# رمز البنك في الآيبان السعودي (الخانتان 5-6) ← الرمز المختصر للبنك (SWIFT)
SAUDI_BANKS = {
    "05": ("INMA", "مصرف الإنماء"),
    "10": ("NCBK", "البنك الأهلي السعودي"),
    "15": ("ALBI", "بنك البلاد"),
    "20": ("RIBL", "بنك الرياض"),
    "30": ("ARNB", "البنك العربي الوطني"),
    "40": ("SABB", "البنك السعودي الأول"),
    "45": ("SABB", "البنك السعودي الأول"),
    "50": ("SABB", "البنك السعودي الأول"),
    "55": ("BSFR", "البنك السعودي الفرنسي"),
    "60": ("BJAZ", "بنك الجزيرة"),
    "65": ("SIBC", "البنك السعودي للاستثمار"),
    "80": ("RJHI", "مصرف الراجحي"),
}

HEADERS = [
    "Employee Name / اسم الموظف", "Employee ID / رقم الهوية أو الإقامة", "Bank Code / رمز البنك",
    "IBAN / الآيبان", "Net Salary / صافي الراتب", "Basic Salary / الأساسي", "Housing Allowance / بدل السكن",
    "Other Earnings / بدلات وإضافات أخرى", "Deductions / الاستقطاعات", "Employee No / الرقم الوظيفي",
    "Notes / ملاحظات",
]


def bank_code(iban):
    iban = (iban or "").replace(" ", "").upper()
    if iban.startswith("SA") and len(iban) == 24:
        return SAUDI_BANKS.get(iban[4:6], ("", ""))[0]
    return ""


def wps_rows(run):
    """صفوف الملف مع ملاحظات التحقق: الهوية 10 أرقام تبدأ بـ 1 (مواطن) أو 2 (مقيم)، والآيبان سعودي ومعروف البنك."""
    rows, problems = [], 0
    for line in run.lines.select_related("employee").filter(net_pay__gt=0).order_by("employee__employee_number"):
        emp = line.employee
        notes = []
        if not re.fullmatch(r"[12]\d{9}", emp.national_id or ""):
            notes.append("رقم الهوية/الإقامة يجب أن يكون 10 أرقام يبدأ بـ 1 أو 2")
        code = bank_code(emp.iban)
        if not emp.iban:
            notes.append("لا يوجد آيبان")
        elif not code:
            notes.append("رمز البنك غير معروف من الآيبان؛ أدخله يدوياً")
        basic = line.basic
        housing = line.housing
        deductions = line.total_deduction
        # كل ما سوى الأساسي والسكن (النقل، البدلات، العمل الإضافي، الإضافات) بحيث يطابق الصافي = الإجمالي − الاستقطاعات
        other = line.net_pay + deductions - basic - housing
        problems += bool(notes)
        rows.append([emp.full_name, emp.national_id, code, emp.iban, line.net_pay, basic, housing, other, deductions,
                     emp.employee_number, "؛ ".join(notes)])
    return rows, problems


@login_required
def wps_file(request, pk):
    from hr.models import PayrollRun

    run = get_object_or_404(PayrollRun, pk=pk)
    if run.docstatus != DocStatus.SUBMITTED:
        messages.error(request, "ملف حماية الأجور متاح للمسيرات المعتمدة فقط.")
        return redirect(run.get_absolute_url())
    rows, problems = wps_rows(run)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow([csv_safe(value) if isinstance(value, str) else f"{value:.2f}" for value in row])
    response = HttpResponse("﻿" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="wps-{run.year}-{run.month:02d}.csv"'
    if problems:
        response["X-WPS-Problems"] = str(problems)
    return response
