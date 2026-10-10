"""معالجة البصمات الخام (من الأجهزة أو الملفات) وتحويلها لسجلات حضور يومية."""
import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from hr.models import (Attendance, AttendancePunch, AttendanceStatus, Employee, PayrollSettings, PunchSource,
                       shift_on)


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


def employees_by_pin():
    mapping = {}
    for emp in Employee.objects.all():
        mapping.setdefault(emp.employee_number, emp)
        if emp.device_id:
            mapping[emp.device_id] = emp
    return mapping


def _aware(moment):
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return moment.replace(second=0, microsecond=0)


def recompute_day(employee, day):
    """يعيد بناء سجل حضور اليوم من كل بصماته: أول بصمة حضور وآخر بصمة انصراف."""
    start = timezone.make_aware(datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time()))
    end = start + datetime.timedelta(days=3)
    times = sorted(timezone.localtime(p.punched_at) for p in employee.punches.filter(
        punched_at__gte=start, punched_at__lt=end) if work_date(employee, timezone.localtime(p.punched_at)) == day)
    if not times:
        return None
    record = Attendance.objects.filter(employee=employee, attendance_date=day).first()
    created = record is None
    if created:
        record = Attendance(employee=employee, attendance_date=day)
    if record.status == AttendanceStatus.ABSENT:
        record.status = AttendanceStatus.PRESENT
    record.check_in = times[0].time()
    record.check_out = times[-1].time() if len(times) > 1 and times[-1] > times[0] else record.check_out
    record.save()
    return "created" if created else "updated"


def record_punches(entries, source=PunchSource.DEVICE, device=None):
    """entries: [(رقم البصمة، الوقت)]. يحفظ الجديد منها فقط ثم يحدّث أيام الحضور المتأثرة."""
    mapping = employees_by_pin()
    existing = set()
    stored, unknown, days = 0, set(), {}
    for pin, moment in entries:
        pin = str(pin).strip()
        if not pin:
            continue
        moment = _aware(moment)
        if (pin, moment) in existing:
            continue
        existing.add((pin, moment))
        employee = mapping.get(pin)
        punch, created = AttendancePunch.objects.get_or_create(
            pin=pin, punched_at=moment,
            defaults={"employee": employee, "device": device, "source": source,
                      "error": "" if employee else "رقم بصمة غير مرتبط بموظف"})
        if not created:
            continue
        stored += 1
        if employee is None:
            unknown.add(pin)
            continue
        days.setdefault((employee.pk, work_date(employee, timezone.localtime(moment))), (employee, []))[1].append(punch)
    result = {"stored": stored, "duplicates": len(existing) - stored - 0, "unknown": sorted(unknown),
              "created": 0, "updated": 0, "skipped": []}
    for (_, day), (employee, punches) in sorted(days.items(), key=lambda kv: kv[0][1]):
        try:
            with transaction.atomic():
                outcome = recompute_day(employee, day)
        except ValidationError as exc:
            message = " ".join(exc.messages)
            AttendancePunch.objects.filter(pk__in=[p.pk for p in punches]).update(error=message[:255])
            result["skipped"].append(f"{employee} {day:%Y-%m-%d}: {message}")
            continue
        if outcome:
            result[outcome] += 1
    result["duplicates"] = len(existing) - stored
    return result


def reprocess_unknown():
    """بعد ربط رقم بصمة بموظف: تُعالج بصماته السابقة غير المرتبطة."""
    mapping = employees_by_pin()
    pending = AttendancePunch.objects.filter(employee__isnull=True)
    days = {}
    for punch in pending:
        employee = mapping.get(punch.pin)
        if employee is None:
            continue
        punch.employee, punch.error = employee, ""
        punch.save(update_fields=["employee", "error"])
        days[(employee.pk, work_date(employee, timezone.localtime(punch.punched_at)))] = employee
    for (_, day), employee in days.items():
        try:
            with transaction.atomic():
                recompute_day(employee, day)
        except ValidationError:
            continue
    return len(days)
