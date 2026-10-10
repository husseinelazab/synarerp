import datetime
import io

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.forms import formfield_callback, style_form
from hr.models import (DocStatus, Employee, LeaveType, PayrollRun, PayrollSettings, overlap_days,
                       used_leave_days)


class PayrollSettingsForm(forms.ModelForm):
    class Meta:
        model = PayrollSettings
        exclude = ["company"]
        formfield_callback = formfield_callback


@login_required
def payroll_settings(request):
    instance = PayrollSettings.for_company(request.company)
    form = style_form(PayrollSettingsForm(request.POST or None, instance=instance))
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ إعدادات الرواتب.")
        return redirect("payroll_settings")
    return render(request, "hr/settings.html", {"form": form})


@login_required
def leave_balances(request):
    today = datetime.date.today()
    try:
        year = int(request.GET.get("year", today.year))
    except ValueError:
        year = today.year
    types = list(LeaveType.objects.filter(disabled=False))
    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    rows = []
    for emp in Employee.objects.filter(date_of_joining__lte=end).exclude(relieving_date__lt=start):
        cells = []
        for lt in types:
            used = used_leave_days(emp, lt, year)
            cells.append({"used": used, "allowed": lt.days_per_year,
                          "remaining": lt.days_per_year - used if lt.days_per_year else None})
        rows.append({"employee": emp, "cells": cells})
    return render(request, "hr/leave_balances.html", {
        "year": year, "types": types, "rows": rows, "years": range(today.year - 3, today.year + 2),
    })


@login_required
def payslips(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk)
    lines = run.lines.select_related("employee", "employee__department", "employee__designation")
    if request.GET.get("line"):
        lines = lines.filter(pk=request.GET["line"])
    return render(request, "hr/payslips.html", {"run": run, "lines": lines, "company": request.company})


@login_required
def bank_file(request, pk):
    """ملف تحويل الرواتب للبنك (Excel): الاسم، الهوية، البنك، الآيبان، صافي الراتب."""
    import openpyxl
    from openpyxl.styles import Font

    run = get_object_or_404(PayrollRun, pk=pk)
    if run.docstatus != DocStatus.SUBMITTED:
        messages.error(request, "ملف البنك متاح للمسيرات المعتمدة فقط.")
        return redirect(run.get_absolute_url())
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "تحويل الرواتب"
    sheet.sheet_view.rightToLeft = True
    sheet.append(["#", "الرقم الوظيفي", "اسم الموظف", "رقم الهوية / الإقامة", "البنك", "الآيبان",
                  "صافي الراتب", "البيان"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    missing = 0
    for i, line in enumerate(run.lines.select_related("employee").filter(net_pay__gt=0), start=1):
        emp = line.employee
        missing += not emp.iban
        sheet.append([i, emp.employee_number, emp.full_name, emp.national_id, emp.bank_name, emp.iban,
                      float(line.net_pay), f"راتب {run.period_label}"])
    sheet.append([])
    sheet.append(["", "", "الإجمالي", "", "", "", float(run.total_net)])
    for column, width in zip("ABCDEFGH", (5, 14, 30, 18, 18, 30, 14, 20)):
        sheet.column_dimensions[column].width = width
    output = io.BytesIO()
    workbook.save(output)
    response = HttpResponse(output.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="payroll-{run.year}-{run.month:02d}.xlsx"'
    if missing:
        response["X-Missing-IBAN"] = str(missing)
    return response

