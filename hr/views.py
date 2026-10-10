import datetime
import io
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.forms import formfield_callback, style_form
from django.core.exceptions import ValidationError
from django.db import transaction

from hr.eos import calculate, describe_duration
from hr.models import (MONTHS, Attendance, AttendanceStatus, DocStatus, Employee, LeaveApplication, LeaveType,
                       PayrollRun, PayrollSettings, eos_provision_balance, month_bounds, used_leave_days)


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
    groups = [
        ("الحسابات", [f for f in form if "account" in f.name]),
        ("التأمينات الاجتماعية وأجر اليوم", [f for f in form if f.name.startswith("gosi") or f.name == "days_per_month"]),
        ("مكافأة نهاية الخدمة", [f for f in form if f.name in ("accrue_eos", "eos_wage_basis")]),
        ("الدوام والحضور والعمل الإضافي", [f for f in form if f.name in (
            "work_start", "work_end", "hours_per_day", "grace_minutes", "weekend", "deduct_absence", "deduct_late",
            "overtime_premium")]),
    ]
    return render(request, "hr/settings.html", {"form": form, "groups": groups})


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



def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValidationError(f"وقت غير صحيح: {value}")


def _parse_hours(value):
    try:
        return Decimal(value or 0)
    except InvalidOperation:
        raise ValidationError(f"عدد ساعات غير صحيح: {value}")


@login_required
def attendance_daily(request):
    """تسجيل حضور جميع الموظفين ليوم واحد في شاشة واحدة."""
    try:
        day = datetime.date.fromisoformat(request.GET.get("date") or request.POST.get("date") or "")
    except ValueError:
        day = datetime.date.today()
    settings = PayrollSettings.for_company(request.company)
    employees = list(Employee.objects.filter(date_of_joining__lte=day).exclude(relieving_date__lt=day)
                     .select_related("department"))
    records = {a.employee_id: a for a in Attendance.objects.filter(attendance_date=day)}
    leaves = {lv.employee_id: lv for lv in LeaveApplication.objects.filter(
        docstatus=DocStatus.SUBMITTED, from_date__lte=day, to_date__gte=day).select_related("leave_type")}
    errors = {}
    if request.method == "POST":
        saved = 0
        for emp in employees:
            if emp.id in leaves:
                continue
            status = request.POST.get(f"status_{emp.id}", "")
            record = records.get(emp.id)
            try:
                with transaction.atomic():
                    if not status:
                        if record is not None:
                            record.delete()
                            records.pop(emp.id)
                            saved += 1
                        continue
                    record = record or Attendance(employee=emp, attendance_date=day)
                    values = (status, _parse_time(request.POST.get(f"in_{emp.id}")),
                              _parse_time(request.POST.get(f"out_{emp.id}")),
                              _parse_hours(request.POST.get(f"ot_{emp.id}")))
                    if record.pk and values == (record.status, record.check_in, record.check_out, record.overtime_hours):
                        continue
                    record.status, record.check_in, record.check_out, record.overtime_hours = values
                    record.save()
                    records[emp.id] = record
                    saved += 1
            except ValidationError as exc:
                errors[emp.id] = " ".join(exc.messages)
        if errors:
            messages.error(request, f"تعذر حفظ {len(errors)} سجل — راجع الأسطر المعلّمة.")
        else:
            messages.success(request, f"تم حفظ الحضور ليوم {day:%Y-%m-%d} ({saved} تعديل).")
            return redirect(f"{request.path}?date={day:%Y-%m-%d}")
    rows = []
    for emp in employees:
        record = records.get(emp.id)
        posted = request.method == "POST" and emp.id in errors
        rows.append({
            "employee": emp, "record": record, "leave": leaves.get(emp.id), "error": errors.get(emp.id),
            "status": request.POST.get(f"status_{emp.id}") if posted else (record.status if record else ""),
            "check_in": request.POST.get(f"in_{emp.id}") if posted else (
                record.check_in.strftime("%H:%M") if record and record.check_in else ""),
            "check_out": request.POST.get(f"out_{emp.id}") if posted else (
                record.check_out.strftime("%H:%M") if record and record.check_out else ""),
            "overtime": request.POST.get(f"ot_{emp.id}") if posted else (
                record.overtime_hours if record and record.overtime_hours else ""),
        })
    return render(request, "hr/attendance_daily.html", {
        "day": day, "rows": rows, "statuses": AttendanceStatus.choices, "settings": settings,
        "is_weekend": day.weekday() in settings.weekend_days,
        "prev": day - datetime.timedelta(days=1), "next": day + datetime.timedelta(days=1),
        "default_in": settings.work_start.strftime("%H:%M"), "default_out": settings.work_end.strftime("%H:%M"),
    })


@login_required
def attendance_monthly(request):
    today = datetime.date.today()
    try:
        year, month = int(request.GET.get("year", today.year)), int(request.GET.get("month", today.month))
        start, end = month_bounds(year, month)
    except ValueError:
        year, month = today.year, today.month
        start, end = month_bounds(year, month)
    settings = PayrollSettings.for_company(request.company)
    days = [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]
    weekend = settings.weekend_days
    employees = Employee.objects.filter(date_of_joining__lte=end).exclude(relieving_date__lt=start)
    records = {}
    for a in Attendance.objects.filter(attendance_date__range=(start, end)):
        records[(a.employee_id, a.attendance_date)] = a
    leave_days = {}
    for lv in LeaveApplication.objects.filter(docstatus=DocStatus.SUBMITTED, from_date__lte=end, to_date__gte=start):
        for d in days:
            if lv.from_date <= d <= lv.to_date:
                leave_days[(lv.employee_id, d)] = lv
    symbols = {"present": "ح", "absent": "غ", "half_day": "½"}
    rows = []
    for emp in employees:
        cells, totals = [], {"present": 0, "absent": Decimal(0), "leave": 0, "late": 0, "overtime": Decimal(0)}
        for d in days:
            record, leave = records.get((emp.id, d)), leave_days.get((emp.id, d))
            employed = emp.employed_between(d, d) is not None
            if not employed:
                cells.append({"text": "", "css": "off"})
            elif leave:
                totals["leave"] += 1
                cells.append({"text": "إ", "css": "leave", "title": str(leave.leave_type)})
            elif record:
                css = record.status
                if record.status == AttendanceStatus.ABSENT:
                    totals["absent"] += 1
                elif record.status == AttendanceStatus.HALF_DAY:
                    totals["absent"] += Decimal("0.5")
                    totals["present"] += 1
                else:
                    totals["present"] += 1
                totals["late"] += record.late_minutes + record.early_exit_minutes
                totals["overtime"] += record.overtime_hours
                if record.late_minutes:
                    css += " late"
                title = " ".join(filter(None, [
                    record.get_status_display(),
                    f"{record.check_in:%H:%M}" if record.check_in else "",
                    f"– {record.check_out:%H:%M}" if record.check_out else "",
                    f"تأخير {record.late_minutes} د" if record.late_minutes else ""]))
                cells.append({"text": symbols[record.status], "css": css, "title": title})
            elif d.weekday() in weekend:
                cells.append({"text": "", "css": "weekend"})
            else:
                cells.append({"text": "·", "css": "missing", "title": "لم يُسجل"})
        rows.append({"employee": emp, "cells": cells, "totals": totals})
    return render(request, "hr/attendance_monthly.html", {
        "year": year, "month": month, "months": MONTHS, "days": days, "weekend": weekend, "rows": rows,
        "years": range(today.year - 3, today.year + 2),
    })


@login_required
def eos_provision(request):
    """الالتزام المتوقع لمكافأة نهاية الخدمة لكل موظف مقابل المخصص المكوّن."""
    try:
        as_of = datetime.date.fromisoformat(request.GET.get("as_of", ""))
    except ValueError:
        as_of = datetime.date.today()
    settings = PayrollSettings.for_company(request.company)
    rows, totals = [], {"liability": Decimal(0), "provision": Decimal(0), "resign": Decimal(0)}
    for emp in Employee.objects.filter(date_of_joining__lte=as_of).exclude(relieving_date__lt=as_of).exclude(
            settlements__docstatus=DocStatus.SUBMITTED):
        wage = settings.eos_wage(emp)
        full = calculate(emp.date_of_joining, as_of, wage)
        resign = calculate(emp.date_of_joining, as_of, wage, "resignation")
        provision = eos_provision_balance(emp)
        rows.append({"employee": emp, "duration": describe_duration(emp.date_of_joining, as_of), "years": full.years,
                     "wage": wage, "liability": full.award, "resign": resign.award, "provision": provision,
                     "difference": full.award - provision})
        totals["liability"] += full.award
        totals["resign"] += resign.award
        totals["provision"] += provision
    totals["difference"] = totals["liability"] - totals["provision"]
    return render(request, "hr/eos_provision.html", {"rows": rows, "totals": totals, "as_of": as_of,
                                                     "settings": settings})
