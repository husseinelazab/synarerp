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
from django.views.decorators.http import require_POST

from hr.models import (FIXED_HOLIDAYS, MONTHS, Attendance, AttendanceStatus, DocStatus, Employee, Holiday,
                       LeaveApplication, LeaveType, PayrollRun, PayrollSettings, eos_provision_balance, holiday_dates,
                       holiday_on, month_bounds, shift_on, used_leave_days)


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
                     .select_related("department", "shift"))
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
        shift_start, shift_end, _ = settings.schedule(emp, day)
        rows.append({
            "shift": shift_on(emp, day),
            "default_in": shift_start.strftime("%H:%M"), "default_out": shift_end.strftime("%H:%M"),
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
        "is_weekend": day.weekday() in settings.weekend_days, "holiday": holiday_on(day),
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
    holidays = holiday_dates(start, end)
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
            elif d in holidays:
                cells.append({"text": "ع", "css": "holiday", "title": "عطلة رسمية"})
            elif d.weekday() in weekend:
                cells.append({"text": "", "css": "weekend"})
            else:
                cells.append({"text": "·", "css": "missing", "title": "لم يُسجل"})
        rows.append({"employee": emp, "cells": cells, "totals": totals})
    return render(request, "hr/attendance_monthly.html", {
        "year": year, "month": month, "months": MONTHS, "days": days, "weekend": weekend, "rows": rows,
        "holidays": holidays,
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


@login_required
@require_POST
def add_fixed_holidays(request):
    year = datetime.date.today().year
    added = 0
    for name, month, day in FIXED_HOLIDAYS:
        date = datetime.date(year, month, day)
        if not Holiday.objects.filter(from_date__lte=date, to_date__gte=date).exists():
            Holiday.objects.create(name=name, from_date=date, to_date=date)
            added += 1
    messages.success(request, f"أُضيفت {added} عطلة لسنة {year}. أضف عيدي الفطر والأضحى يدوياً حسب التقويم الهجري المعتمد.")
    return redirect("doc_list", slug="holiday")


ID_HEADERS = {"رقم البصمة", "رقم الموظف في جهاز البصمة", "الرقم الوظيفي", "رقم الموظف", "ac-no.", "ac-no", "no.", "id",
              "user id", "userid", "employee id", "emp id", "enroll id", "person id"}
DATETIME_HEADERS = {"التاريخ والوقت", "الوقت والتاريخ", "datetime", "date time", "date/time", "check time", "time stamp",
                    "timestamp", "punch time", "clock time"}
DATE_HEADERS = {"التاريخ", "date"}
TIME_HEADERS = {"الوقت", "time"}


def _norm(header):
    return str(header or "").strip().lower()


def _to_datetime(value, time_value=None):
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, datetime.date):
        moment = datetime.datetime.combine(value, datetime.time())
    else:
        text = str(value or "").strip().replace("/", "-")
        moment = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d",
                    "%d-%m-%Y", "%m-%d-%Y %I:%M %p", "%Y-%m-%d %I:%M %p", "%d-%m-%Y %I:%M %p", "%Y-%m-%dT%H:%M:%S"):
            try:
                moment = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            raise ValueError(text)
    if time_value not in (None, ""):
        if isinstance(time_value, datetime.time):
            clock = time_value
        elif isinstance(time_value, datetime.datetime):
            clock = time_value.time()
        else:
            text = str(time_value).strip()
            for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p"):
                try:
                    clock = datetime.datetime.strptime(text, fmt).time()
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(text)
        moment = datetime.datetime.combine(moment.date(), clock)
    return moment


def parse_punches(filename, content):
    """قراءة سجل بصمات: عمود لرقم الموظف وعمود للتاريخ والوقت (أو عمودان منفصلان للتاريخ والوقت)."""
    from core.importer import read_table

    headers, rows = read_table(filename, content)
    names = [_norm(h) for h in headers]
    find = lambda options: next((i for i, h in enumerate(names) if h in options), None)  # noqa: E731
    id_col, dt_col, date_col, time_col = find(ID_HEADERS), find(DATETIME_HEADERS), find(DATE_HEADERS), find(TIME_HEADERS)
    if id_col is None or (dt_col is None and date_col is None):
        raise ValidationError("لم يُعثر على الأعمدة المطلوبة: «رقم البصمة» (أو الرقم الوظيفي) و«التاريخ والوقت» "
                              "(أو عمودا «التاريخ» و«الوقت»).")
    punches, bad = [], []
    for number, row in enumerate(rows, start=2):
        ident = row[id_col] if id_col < len(row) else None
        if isinstance(ident, float) and ident.is_integer():
            ident = int(ident)
        ident = str(ident or "").strip()
        try:
            if dt_col is not None:
                moment = _to_datetime(row[dt_col])
            else:
                moment = _to_datetime(row[date_col], row[time_col] if time_col is not None else None)
        except (ValueError, IndexError) as exc:
            bad.append(f"السطر {number}: تاريخ/وقت غير مفهوم «{exc}»")
            continue
        if ident:
            punches.append((ident, moment))
    return punches, bad


def work_date(employee, moment):
    """يوم العمل الذي تخصه البصمة: بصمات الصباح الباكر لموظف وردية ليلية تتبع اليوم السابق."""
    previous = moment.date() - datetime.timedelta(days=1)
    shift = shift_on(employee, previous)
    if shift is not None and shift.is_overnight:
        # تُنسب البصمة لما هي أقرب إليه: نهاية وردية الليلة السابقة أم بداية دوام اليوم نفسه
        minutes = moment.hour * 60 + moment.minute
        today = shift_on(employee, moment.date())
        today_start = today.start_time if today is not None else PayrollSettings.for_company(employee.company).work_start
        to_previous_end = abs(minutes - (shift.end_time.hour * 60 + shift.end_time.minute))
        to_today_start = abs(minutes - (today_start.hour * 60 + today_start.minute))
        if to_previous_end < to_today_start:
            return previous
    return moment.date()


@login_required
def attendance_punches(request):
    """استيراد سجل البصمات الخام: أول بصمة في اليوم حضور وآخر بصمة انصراف."""
    result = None
    if request.method == "POST" and request.FILES.get("file"):
        upload = request.FILES["file"]
        try:
            punches, bad = parse_punches(upload.name, upload.read())
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("attendance_punches")
        employees = {}
        for emp in Employee.objects.all():
            employees.setdefault(emp.employee_number, emp)
            if emp.device_id:
                employees[emp.device_id] = emp
        grouped, unknown = {}, set()
        for ident, moment in punches:
            emp = employees.get(ident)
            if emp is None:
                unknown.add(ident)
                continue
            moment = moment.replace(second=0, microsecond=0)
            grouped.setdefault((emp, work_date(emp, moment)), []).append(moment)
        created = updated = 0
        skipped = list(bad)
        for (emp, day), times in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0].employee_number)):
            times.sort()
            check_in = times[0].time()
            check_out = times[-1].time() if len(times) > 1 and times[-1] > times[0] else None
            record = Attendance.objects.filter(employee=emp, attendance_date=day).first()
            try:
                with transaction.atomic():
                    if record is None:
                        record = Attendance(employee=emp, attendance_date=day)
                        created += 1
                    else:
                        updated += 1
                    record.status = AttendanceStatus.PRESENT if record.status == AttendanceStatus.ABSENT else record.status
                    record.check_in, record.check_out = check_in, check_out or record.check_out
                    record.save()
            except ValidationError as exc:
                if record.pk is None:
                    created -= 1
                else:
                    updated -= 1
                skipped.append(f"{emp} {day:%Y-%m-%d}: {' '.join(exc.messages)}")
        result = {"punches": len(punches), "days": len(grouped), "created": created, "updated": updated,
                  "unknown": sorted(unknown), "skipped": skipped}
        if created or updated:
            messages.success(request, f"تم استيراد {created + updated} يوم حضور من {len(punches)} بصمة.")
    return render(request, "hr/attendance_punches.html", {"result": result})


@login_required
def hr_dashboard(request):
    from django.db.models import Count, Sum

    from hr.models import EmployeeLoan, EmployeeStatus, PayrollLine

    today = datetime.date.today()
    active = Employee.objects.filter(date_of_joining__lte=today).exclude(relieving_date__lt=today).exclude(
        status=EmployeeStatus.LEFT)
    on_leave = LeaveApplication.objects.filter(docstatus=DocStatus.SUBMITTED, from_date__lte=today,
                                               to_date__gte=today).select_related("employee", "leave_type")
    attendance = Attendance.objects.filter(attendance_date=today)
    horizon = today + datetime.timedelta(days=60)
    expiring = []
    for emp in active.filter(models_q_expiry(horizon)):
        for label, date in (("الهوية / الإقامة", emp.id_expiry_date), ("الجواز", emp.passport_expiry_date)):
            if date and date <= horizon:
                expiring.append({"employee": emp, "document": label, "date": date, "days": (date - today).days})
    expiring.sort(key=lambda e: e["date"])
    departments = list(active.values("department__name").annotate(n=Count("id")).order_by("-n"))
    top = max((d["n"] for d in departments), default=0)
    for d in departments:
        d["pct"] = round(d["n"] * 100 / top) if top else 0
    last_run = PayrollRun.objects.filter(docstatus=DocStatus.SUBMITTED).order_by("-year", "-month").first()
    loans = EmployeeLoan.objects.filter(docstatus=DocStatus.SUBMITTED)
    provision = (sum(e.eos_opening_provision for e in active) + (PayrollLine.objects.filter(
        parent__docstatus=DocStatus.SUBMITTED, employee__in=active).aggregate(t=Sum("eos_accrual"))["t"] or 0))
    return render(request, "hr/dashboard.html", {
        "today": today, "headcount": active.count(),
        "saudis": active.filter(is_saudi=True).count(),
        "on_leave": on_leave, "present": attendance.exclude(status=AttendanceStatus.ABSENT).count(),
        "absent": attendance.filter(status=AttendanceStatus.ABSENT).select_related("employee"),
        "late": attendance.filter(late_minutes__gt=0).select_related("employee"),
        "pending_leaves": LeaveApplication.objects.filter(docstatus=DocStatus.DRAFT).select_related("employee", "leave_type"),
        "pending_loans": EmployeeLoan.objects.filter(docstatus=DocStatus.DRAFT).select_related("employee"),
        "expiring": expiring, "departments": departments, "last_run": last_run,
        "loans_outstanding": sum(loan.outstanding for loan in loans), "provision": provision,
    })


def models_q_expiry(horizon):
    from django.db.models import Q

    return Q(id_expiry_date__lte=horizon) | Q(passport_expiry_date__lte=horizon)


class PortalAccessForm(forms.Form):
    username = forms.CharField(label="اسم المستخدم", max_length=150)
    password = forms.CharField(label="كلمة المرور", min_length=8, widget=forms.PasswordInput(render_value=False),
                               help_text="8 أحرف على الأقل؛ سلّمها للموظف ليغيّرها لاحقاً")


@login_required
def portal_access(request, pk):
    """إنشاء أو إدارة حساب الموظف في بوابة الخدمة الذاتية (لا يُحسب ضمن مستخدمي الباقة)."""
    from django.contrib.auth.models import User

    from core.models import Membership, Role

    employee = get_object_or_404(Employee, pk=pk)
    membership = (Membership.objects.filter(user=employee.user, company=request.company).first()
                  if employee.user_id else None)
    form = style_form(PortalAccessForm(request.POST or None, initial={
        "username": employee.user.username if employee.user_id else employee.employee_number.lower()}))
    if employee.user_id:
        form.fields["username"].disabled = True
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action in ("disable", "enable") and membership is not None:
            membership.is_active = action == "enable"
            membership.save(update_fields=["is_active"])
            messages.success(request, "تم إيقاف دخول الموظف." if action == "disable" else "تم تفعيل دخول الموظف.")
            return redirect(employee.get_absolute_url())
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                if employee.user_id:
                    user = employee.user
                    user.set_password(data["password"])
                    user.save(update_fields=["password"])
                    messages.success(request, "تم تغيير كلمة مرور الموظف.")
                else:
                    if User.objects.filter(username=data["username"]).exists():
                        form.add_error("username", "اسم المستخدم مستخدم مسبقاً.")
                        return render(request, "hr/portal_access.html", {"form": form, "employee": employee})
                    first, _, last = employee.full_name.partition(" ")
                    user = User.objects.create_user(data["username"], employee.email, data["password"],
                                                    first_name=first, last_name=last)
                    employee.user = user
                    employee.save(update_fields=["user"])
                    messages.success(request, f"تم إنشاء حساب الموظف «{user.username}». يدخل من صفحة تسجيل الدخول نفسها.")
                if membership is None:
                    Membership.objects.create(user=user, company=request.company, role=Role.EMPLOYEE)
            return redirect(employee.get_absolute_url())
    return render(request, "hr/portal_access.html", {"form": form, "employee": employee, "membership": membership})
