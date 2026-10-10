import calendar
import datetime
import math
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

from core.models import DocStatus, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt
from hr.eos import SeparationReason


class Department(TenantModel):
    name = models.CharField("اسم القسم", max_length=100)
    parent = models.ForeignKey("self", models.PROTECT, null=True, blank=True, verbose_name="القسم الأم")

    class Meta:
        verbose_name = "قسم"
        verbose_name_plural = "الأقسام"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="department")]

    def __str__(self):
        return self.name


class Designation(TenantModel):
    name = models.CharField("المسمى الوظيفي", max_length=100)

    class Meta:
        verbose_name = "مسمى وظيفي"
        verbose_name_plural = "المسميات الوظيفية"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="designation")]

    def __str__(self):
        return self.name


class WorkShift(TenantModel):
    """وردية عمل بأوقاتها؛ الموظف بلا وردية يتبع أوقات الدوام في إعدادات الرواتب."""

    name = models.CharField("اسم الوردية", max_length=100)
    start_time = models.TimeField("بداية الدوام")
    end_time = models.TimeField("نهاية الدوام")
    grace_minutes = models.PositiveSmallIntegerField("دقائق السماح للتأخير", default=15)
    hours_per_day = models.DecimalField("ساعات العمل اليومية", max_digits=4, decimal_places=2, default=8)

    class Meta:
        verbose_name = "وردية عمل"
        verbose_name_plural = "ورديات العمل"
        ordering = ["start_time"]
        constraints = [tenant_unique("name", model_label="workshift")]

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M}–{self.end_time:%H:%M})"

    def clean(self):
        if self.start_time and self.end_time and self.end_time == self.start_time:
            raise ValidationError({"end_time": "نهاية الدوام يجب أن تختلف عن بدايته."})

    @property
    def is_overnight(self):
        """الوردية الليلية تنتهي في اليوم التالي (مثل 22:00 – 06:00)."""
        return self.end_time < self.start_time


class ShiftAssignment(TenantModel):
    """تدوير الورديات: وردية مؤقتة للموظف في فترة تتقدم على ورديته الأساسية."""

    employee = models.ForeignKey("hr.Employee", models.CASCADE, related_name="shift_assignments", verbose_name="الموظف")
    shift = models.ForeignKey(WorkShift, models.PROTECT, verbose_name="الوردية")
    from_date = models.DateField("من تاريخ")
    to_date = models.DateField("إلى تاريخ", null=True, blank=True, help_text="يُترك فارغاً للاستمرار حتى إشعار آخر")

    class Meta:
        verbose_name = "تكليف وردية"
        verbose_name_plural = "جدول الورديات"
        ordering = ["-from_date"]

    def __str__(self):
        return f"{self.employee} ← {self.shift.name}"

    def clean(self):
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValidationError({"to_date": "تاريخ النهاية يجب أن يكون بعد تاريخ البداية أو يساويه."})
        if self.employee_id and self.from_date:
            clash = ShiftAssignment.objects.filter(employee_id=self.employee_id).exclude(pk=self.pk).filter(
                models.Q(to_date__isnull=True) | models.Q(to_date__gte=self.from_date))
            if self.to_date:
                clash = clash.filter(from_date__lte=self.to_date)
            if clash.exists():
                raise ValidationError("تتداخل هذه الفترة مع تكليف وردية آخر للموظف.")


def shift_on(employee, date):
    """وردية الموظف في تاريخ: التكليف المؤقت إن وجد، وإلا ورديته الأساسية (أو None للدوام العام)."""
    assignment = ShiftAssignment.objects.filter(employee=employee, from_date__lte=date).filter(
        models.Q(to_date__isnull=True) | models.Q(to_date__gte=date)).select_related("shift").first()
    return assignment.shift if assignment else employee.shift


class Holiday(TenantModel):
    """العطل الرسمية: لا تُحتسب من رصيد الإجازات ولا يُسجل فيها غياب."""

    name = models.CharField("العطلة", max_length=100)
    from_date = models.DateField("من تاريخ")
    to_date = models.DateField("إلى تاريخ")

    class Meta:
        verbose_name = "عطلة رسمية"
        verbose_name_plural = "العطل الرسمية"
        ordering = ["-from_date"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValidationError({"to_date": "تاريخ النهاية يجب أن يكون بعد تاريخ البداية أو يساويه."})

    @property
    def total_days(self):
        return (self.to_date - self.from_date).days + 1


# العطل الثابتة بالتاريخ الميلادي؛ عيدا الفطر والأضحى يتغيران سنوياً (هجرية) فتُضافان يدوياً
FIXED_HOLIDAYS = [("يوم التأسيس", 2, 22), ("اليوم الوطني", 9, 23)]


def add_official_holidays(year):
    """يضيف يوم التأسيس واليوم الوطني وعيدي الفطر والأضحى (تقويم أم القرى) للسنة إن لم تكن مسجلة."""
    from core.hijri import eid_holidays

    items = [(name, datetime.date(year, month, day), datetime.date(year, month, day))
             for name, month, day in FIXED_HOLIDAYS] + eid_holidays(year)
    added = []
    for name, start, end in items:
        if not Holiday.objects.filter(from_date__lte=end, to_date__gte=start).exists():
            added.append(Holiday.objects.create(name=name, from_date=start, to_date=end))
    return added


def holiday_dates(start, end):
    dates = set()
    for h in Holiday.objects.filter(from_date__lte=end, to_date__gte=start):
        day = max(h.from_date, start)
        while day <= min(h.to_date, end):
            dates.add(day)
            day += datetime.timedelta(days=1)
    return dates


def holiday_on(date):
    return Holiday.objects.filter(from_date__lte=date, to_date__gte=date).first()


def leave_days_between(start, end, holidays=None):
    """أيام الإجازة في الفترة بعد استبعاد العطل الرسمية (المادة 112)."""
    if end < start:
        return 0
    holidays = holiday_dates(start, end) if holidays is None else holidays
    return (end - start).days + 1 - len([d for d in holidays if start <= d <= end])


class Gender(models.TextChoices):
    MALE = "male", "ذكر"
    FEMALE = "female", "أنثى"


class EmployeeStatus(models.TextChoices):
    ACTIVE = "active", "على رأس العمل"
    LEAVE = "leave", "في إجازة"
    LEFT = "left", "منتهية خدماته"


class Employee(TenantModel):
    employee_number = models.CharField("الرقم الوظيفي", max_length=20)
    full_name = models.CharField("الاسم الكامل", max_length=200)
    gender = models.CharField("الجنس", max_length=10, choices=Gender.choices)
    nationality = models.CharField("الجنسية", max_length=50, blank=True)
    is_saudi = models.BooleanField("سعودي الجنسية", default=False,
                                   help_text="يحدد نسب التأمينات الاجتماعية (حصة الموظف تُستقطع من السعوديين فقط)")
    gosi_subscribed = models.BooleanField("مشترك في التأمينات الاجتماعية", default=True)
    national_id = models.CharField("رقم الهوية / الإقامة", max_length=20, blank=True)
    date_of_birth = models.DateField("تاريخ الميلاد", null=True, blank=True)
    date_of_joining = models.DateField("تاريخ التعيين")
    relieving_date = models.DateField("تاريخ انتهاء الخدمة", null=True, blank=True)
    department = models.ForeignKey(Department, models.PROTECT, null=True, blank=True, verbose_name="القسم")
    designation = models.ForeignKey(Designation, models.PROTECT, null=True, blank=True, verbose_name="المسمى الوظيفي")
    basic_salary = models.DecimalField("الراتب الأساسي", max_digits=18, decimal_places=2, default=0)
    housing_allowance = models.DecimalField("بدل السكن", max_digits=18, decimal_places=2, default=0)
    transport_allowance = models.DecimalField("بدل النقل", max_digits=18, decimal_places=2, default=0)
    other_allowance = models.DecimalField("بدلات أخرى", max_digits=18, decimal_places=2, default=0)
    bank_name = models.CharField("البنك", max_length=100, blank=True)
    iban = models.CharField("رقم الآيبان", max_length=34, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    status = models.CharField("الحالة", max_length=10, choices=EmployeeStatus.choices, default=EmployeeStatus.ACTIVE)
    shift = models.ForeignKey(WorkShift, models.PROTECT, null=True, blank=True, verbose_name="وردية العمل",
                              help_text="تُترك فارغة لاتباع أوقات الدوام العامة في إعدادات الرواتب")
    id_expiry_date = models.DateField("انتهاء الهوية / الإقامة", null=True, blank=True)
    passport_number = models.CharField("رقم الجواز", max_length=20, blank=True)
    passport_expiry_date = models.DateField("انتهاء الجواز", null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, editable=False,
                             related_name="employee_records", verbose_name="حساب بوابة الخدمة الذاتية")
    device_id = models.CharField("رقم الموظف في جهاز البصمة", max_length=30, blank=True,
                                 help_text="يُستخدم لمطابقة سجلات البصمة المستوردة (وإن تُرك فارغاً يُطابق بالرقم الوظيفي)")
    eos_opening_provision = models.DecimalField(
        "مخصص نهاية الخدمة المكوّن قبل النظام", max_digits=18, decimal_places=2, default=0,
        help_text="للموظفين القدامى: ما سبق تكوينه لهم في حساب المخصص (ضمن الأرصدة الافتتاحية) قبل استخدام النظام")

    class Meta:
        verbose_name = "موظف"
        verbose_name_plural = "الموظفون"
        ordering = ["employee_number"]
        constraints = [tenant_unique("employee_number", model_label="employee")]

    def __str__(self):
        return f"{self.employee_number} - {self.full_name}"

    def get_absolute_url(self):
        return reverse("doc_detail", args=["employee", self.pk])

    @property
    def gross_salary(self):
        return self.basic_salary + self.housing_allowance + self.transport_allowance + self.other_allowance

    def clean(self):
        if self.relieving_date and self.date_of_joining and self.relieving_date < self.date_of_joining:
            raise ValidationError({"relieving_date": "تاريخ انتهاء الخدمة يجب أن يكون بعد تاريخ التعيين."})
        if self.iban:
            self.iban = self.iban.replace(" ", "").upper()
            if not (self.iban.startswith("SA") and len(self.iban) == 24):
                raise ValidationError({"iban": "الآيبان السعودي يبدأ بـ SA ويتكون من 24 خانة."})

    def hr_summary(self, as_of=None):
        """ملخص لصفحة الموظف: مدة الخدمة، تقدير المكافأة، المخصص، الإجازات، حضور الشهر."""
        from hr.eos import SeparationReason, calculate, describe_duration

        today = as_of or datetime.date.today()
        end = min(today, self.relieving_date) if self.relieving_date else today
        settings = PayrollSettings.for_company(self.company)
        wage = settings.eos_wage(self)
        start, month_end = month_bounds(today.year, today.month)
        return {
            "duration": describe_duration(self.date_of_joining, end),
            "as_of": end,
            "wage": wage,
            "eos_full": calculate(self.date_of_joining, end, wage).award,
            "eos_resign": calculate(self.date_of_joining, end, wage, SeparationReason.RESIGNATION).award,
            "provision": eos_provision_balance(self),
            "leaves": [
                {"type": lt, "used": used_leave_days(self, lt, today.year),
                 "remaining": lt.days_per_year - used_leave_days(self, lt, today.year) if lt.days_per_year else None}
                for lt in LeaveType.objects.filter(disabled=False)
            ],
            "attendance": attendance_summary(self, start, min(month_end, today)),
            "month_label": f"{dict(MONTHS)[today.month]} {today.year}",
            "settled": self.settlements.filter(docstatus=DocStatus.SUBMITTED).first(),
            "loans": open_loans(self),
            "loan_balance": flt(sum(loan.outstanding for loan in open_loans(self))),
        }

    def employed_between(self, start, end):
        """الفترة التي كان فيها الموظف على رأس العمل ضمن [start, end]، أو None."""
        first = max(start, self.date_of_joining)
        last = min(end, self.relieving_date) if self.relieving_date else end
        return (first, last) if first <= last else None


def overlap_days(start, end, other_start, other_end):
    first, last = max(start, other_start), min(end, other_end)
    return (last - first).days + 1 if first <= last else 0


def month_bounds(year, month):
    return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


# ───────────────────────────── الإجازات ─────────────────────────────

DEFAULT_LEAVE_TYPES = [
    # (الاسم، الرصيد السنوي (0 = بلا حد)، مدفوعة)
    ("إجازة سنوية", 21, True),
    ("إجازة مرضية", 30, True),
    ("إجازة بدون راتب", 0, False),
    ("إجازة زواج", 5, True),
    ("إجازة وفاة", 5, True),
    ("إجازة مولود", 3, True),
    ("إجازة امتحانات", 0, True),
]


class LeaveType(TenantModel):
    name = models.CharField("نوع الإجازة", max_length=100)
    days_per_year = models.PositiveIntegerField("الرصيد السنوي (أيام)", default=0, help_text="صفر = بلا حد للرصيد")
    is_paid = models.BooleanField("مدفوعة الأجر", default=True,
                                  help_text="الإجازة غير المدفوعة تُخصم أيامها من الراتب في مسير الشهر")
    encashable = models.BooleanField("يُصرف رصيدها المتبقي عند انتهاء الخدمة", default=False)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "نوع إجازة"
        verbose_name_plural = "أنواع الإجازات"
        ordering = ["id"]
        constraints = [tenant_unique("name", model_label="leavetype")]

    def __str__(self):
        return self.name


def used_leave_days(employee, leave_type, year, exclude_pk=None):
    """الأيام المستهلكة من نوع إجازة في سنة ميلادية (من الطلبات المعتمدة)."""
    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    leaves = LeaveApplication.objects.filter(
        employee=employee, leave_type=leave_type, docstatus=DocStatus.SUBMITTED,
        from_date__lte=end, to_date__gte=start,
    ).exclude(pk=exclude_pk)
    holidays = holiday_dates(start, end)
    return sum(leave_days_between(max(start, lv.from_date), min(end, lv.to_date), holidays) for lv in leaves)


def unpaid_leave_days(employee, start, end):
    """أيام الإجازات غير المدفوعة المعتمدة الواقعة داخل الفترة."""
    leaves = LeaveApplication.objects.filter(
        employee=employee, leave_type__is_paid=False, docstatus=DocStatus.SUBMITTED,
        from_date__lte=end, to_date__gte=start,
    )
    holidays = holiday_dates(start, end)
    return sum(leave_days_between(max(start, lv.from_date), min(end, lv.to_date), holidays) for lv in leaves)


class PortalRequestMixin:
    """طلب يقدمه الموظف من البوابة كمسودة، فيعتمده المدير أو يرفضه."""

    @property
    def is_rejected(self):
        return self.is_cancelled and bool(self.rejection_reason)

    @property
    def request_status(self):
        if self.is_draft:
            return "بانتظار الاعتماد"
        if self.is_rejected:
            return "مرفوض"
        return "معتمد" if self.is_submitted else "ملغى"

    @property
    def draft_actions(self):
        return [("reject", "رفض الطلب")] if self.is_draft else []

    def reject(self, user, reason=""):
        if not self.is_draft:
            raise ValidationError("يمكن رفض الطلبات غير المعتمدة فقط.")
        self.rejection_reason = reason or f"رُفض بواسطة {user.get_full_name() or user.username}"
        self.docstatus = DocStatus.CANCELLED
        self.save(update_fields=["rejection_reason", "docstatus"])
        self.notify_employee(f"رُفض {self.request_label}: {self.rejection_reason}")

    def notify_employee(self, title):
        from core.notify import notify

        if self.requested_via_portal and self.employee.user_id:
            notify(self.company, [self.employee.user], title, reverse("portal_home"))

    def notify_managers_of_request(self):
        from core.notify import notify_managers

        notify_managers(self.company, f"{self.request_label} جديد من {self.employee.full_name} بانتظار الاعتماد",
                        self.get_absolute_url())


class LeaveApplication(PortalRequestMixin, SubmittableDocument):
    naming_prefix = "LV"
    posts_to_ledger = False

    employee = models.ForeignKey(Employee, models.PROTECT, related_name="leaves", verbose_name="الموظف")
    leave_type = models.ForeignKey(LeaveType, models.PROTECT, verbose_name="نوع الإجازة",
                                   limit_choices_to={"disabled": False})
    from_date = models.DateField("من تاريخ")
    to_date = models.DateField("إلى تاريخ")
    total_days = models.PositiveIntegerField("عدد الأيام (بدون العطل الرسمية)", default=0, editable=False)
    requested_via_portal = models.BooleanField("طلب من بوابة الموظف", default=False, editable=False)
    rejection_reason = models.CharField("سبب الرفض", max_length=255, blank=True, editable=False)

    @property
    def request_label(self):
        return f"طلب {self.leave_type} ({self.from_date:%Y-%m-%d} – {self.to_date:%Y-%m-%d})"

    def on_submit(self):
        self.notify_employee(f"اعتُمد {self.request_label}")

    posting_note = "طلب الإجازة لا يُنشئ قيوداً محاسبية؛ أيام الإجازة بدون راتب تُخصم تلقائياً في مسير رواتب الشهر."

    class Meta(SubmittableDocument.Meta):
        verbose_name = "طلب إجازة"
        verbose_name_plural = "طلبات الإجازات"

    def clean(self):
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValidationError({"to_date": "تاريخ النهاية يجب أن يكون بعد تاريخ البداية أو يساويه."})

    def calculate_totals(self):
        if self.from_date and self.to_date and self.to_date >= self.from_date:
            self.total_days = leave_days_between(self.from_date, self.to_date)

    def validate_for_submit(self):
        self.clean()
        employee = self.employee
        if employee.employed_between(self.from_date, self.to_date) != (self.from_date, self.to_date):
            raise ValidationError("فترة الإجازة تقع خارج فترة عمل الموظف (قبل التعيين أو بعد انتهاء الخدمة).")
        clash = LeaveApplication.objects.filter(
            employee=employee, docstatus=DocStatus.SUBMITTED,
            from_date__lte=self.to_date, to_date__gte=self.from_date,
        ).exclude(pk=self.pk).first()
        if clash:
            raise ValidationError(
                f"تتداخل هذه الإجازة مع الطلب المعتمد {clash} ({clash.from_date:%Y-%m-%d} إلى {clash.to_date:%Y-%m-%d})."
            )
        allowed = self.leave_type.days_per_year
        if allowed:
            # يُحسب الرصيد لكل سنة ميلادية تقع فيها أيام الإجازة
            for year in range(self.from_date.year, self.to_date.year + 1):
                start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
                requested = leave_days_between(max(start, self.from_date), min(end, self.to_date))
                used = used_leave_days(employee, self.leave_type, year, exclude_pk=self.pk)
                if used + requested > allowed:
                    raise ValidationError(
                        f"رصيد «{self.leave_type}» لسنة {year} لا يكفي: الرصيد {allowed} يوم، "
                        f"المستهلك {used}، المطلوب {requested}، المتبقي {max(allowed - used, 0)}."
                    )
        processed = PayrollLine.objects.filter(
            employee=employee, parent__docstatus=DocStatus.SUBMITTED,
        ).select_related("parent")
        if not self.leave_type.is_paid:
            for line in processed:
                start, end = month_bounds(line.parent.year, line.parent.month)
                if overlap_days(start, end, self.from_date, self.to_date):
                    raise ValidationError(
                        f"راتب شهر {line.parent.period_label} صُرف في المسير {line.parent}. "
                        "ألغِ المسير أولاً أو سجّل الخصم يدوياً في مسير لاحق."
                    )

    def before_cancel(self):
        if not self.leave_type.is_paid:
            for line in PayrollLine.objects.filter(employee=self.employee, parent__docstatus=DocStatus.SUBMITTED,
                                                   unpaid_days__gt=0).select_related("parent"):
                start, end = month_bounds(line.parent.year, line.parent.month)
                if overlap_days(start, end, self.from_date, self.to_date):
                    raise ValidationError(f"خُصمت أيام هذه الإجازة في المسير {line.parent}. ألغِ المسير أولاً.")


# ───────────────────────────── الحضور والانصراف ─────────────────────────────

class AttendanceStatus(models.TextChoices):
    PRESENT = "present", "حاضر"
    ABSENT = "absent", "غائب"
    HALF_DAY = "half_day", "نصف يوم"


def payroll_locked(employee, date):
    """المسير المعتمد الذي صُرف فيه راتب الموظف لشهر هذا التاريخ، إن وجد."""
    line = PayrollLine.objects.filter(
        employee=employee, parent__year=date.year, parent__month=date.month, parent__docstatus=DocStatus.SUBMITTED,
    ).select_related("parent").first()
    return line.parent if line else None


def _minutes(time):
    return time.hour * 60 + time.minute


class Attendance(TenantModel):
    employee = models.ForeignKey(Employee, models.PROTECT, related_name="attendance", verbose_name="الموظف")
    attendance_date = models.DateField("التاريخ")
    status = models.CharField("الحالة", max_length=10, choices=AttendanceStatus.choices, default=AttendanceStatus.PRESENT)
    check_in = models.TimeField("وقت الحضور", null=True, blank=True)
    check_out = models.TimeField("وقت الانصراف", null=True, blank=True)
    overtime_hours = models.DecimalField("ساعات إضافية معتمدة", max_digits=5, decimal_places=2, default=0,
                                         help_text="تُصرف في مسير الشهر. العمل الإضافي يحتاج موافقة صاحب العمل")
    notes = models.CharField("ملاحظات", max_length=255, blank=True)
    late_minutes = models.PositiveIntegerField("دقائق التأخير", default=0, editable=False)
    early_exit_minutes = models.PositiveIntegerField("دقائق الخروج المبكر", default=0, editable=False)
    worked_hours = models.DecimalField("ساعات العمل الفعلية", max_digits=5, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "سجل حضور"
        verbose_name_plural = "سجل الحضور والانصراف"
        ordering = ["-attendance_date", "employee__employee_number"]
        constraints = [tenant_unique("employee", "attendance_date", model_label="attendance")]

    def __str__(self):
        return f"{self.employee} - {self.attendance_date:%Y-%m-%d}"

    def get_absolute_url(self):
        return reverse("doc_detail", args=[self.doctype_slug, self.pk])

    def validate(self):
        if self.status == AttendanceStatus.ABSENT:
            self.check_in = self.check_out = None
        if self.check_in and self.check_out and self.check_out <= self.check_in and not self.is_overnight:
            raise ValidationError("وقت الانصراف يجب أن يكون بعد وقت الحضور.")
        if self.overtime_hours < 0 or self.overtime_hours > 12:
            raise ValidationError("ساعات العمل الإضافي يجب أن تكون بين 0 و12.")
        employee, date = self.employee, self.attendance_date
        if employee.employed_between(date, date) is None:
            raise ValidationError(f"{employee} لم يكن على رأس العمل في {date:%Y-%m-%d}.")
        leave = LeaveApplication.objects.filter(employee=employee, docstatus=DocStatus.SUBMITTED,
                                                from_date__lte=date, to_date__gte=date).first()
        if leave:
            raise ValidationError(f"{employee} في إجازة معتمدة ({leave}) بتاريخ {date:%Y-%m-%d}.")
        if self.status != AttendanceStatus.PRESENT:
            holiday = holiday_on(date)
            if holiday:
                raise ValidationError(f"{date:%Y-%m-%d} عطلة رسمية ({holiday})؛ لا يُسجل فيها غياب.")
        self.check_lock()

    def check_lock(self, date=None, employee=None):
        run = payroll_locked(employee or self.employee, date or self.attendance_date)
        if run:
            raise ValidationError(f"صُرف راتب هذا الشهر في المسير المعتمد {run}؛ ألغِ المسير لتعديل الحضور.")

    @property
    def is_overnight(self):
        shift = shift_on(self.employee, self.attendance_date)
        return bool(shift and shift.is_overnight)

    def compute(self, settings):
        start, end, grace = settings.schedule(self.employee, self.attendance_date)
        self.late_minutes = self.early_exit_minutes = 0
        self.worked_hours = flt(0)
        overnight = end < start
        start_m = _minutes(start)
        end_m = _minutes(end) + (1440 if overnight else 0)

        def clock(time):
            # في الوردية الليلية: الأوقات بعد منتصف الليل (قبل البداية بأكثر من 6 ساعات) تخص اليوم التالي
            m = _minutes(time)
            return m + 1440 if overnight and m < start_m - 360 else m

        off_day = self.attendance_date.weekday() in settings.weekend_days or holiday_on(self.attendance_date)
        if self.check_in and not off_day:
            # العمل في يوم الراحة أو العطلة لا يُحسب فيه تأخير؛ ساعاته تُعتمد عملاً إضافياً
            late = clock(self.check_in) - start_m
            self.late_minutes = late if late > grace else 0
        if self.check_out and not off_day and self.status == AttendanceStatus.PRESENT:
            self.early_exit_minutes = max(end_m - clock(self.check_out), 0)
        if self.check_in and self.check_out:
            worked = clock(self.check_out) - clock(self.check_in)
            if worked <= 0:
                worked += 1440
            self.worked_hours = flt(Decimal(worked) / 60)

    def save(self, *args, **kwargs):
        if self.pk:
            old = Attendance.all_objects.filter(pk=self.pk).values("employee_id", "attendance_date").first()
            if old and (old["employee_id"], old["attendance_date"]) != (self.employee_id, self.attendance_date):
                self.check_lock(old["attendance_date"], Employee.all_objects.get(pk=old["employee_id"]))
        self.validate()
        super().save(*args, **kwargs)  # يحدد الشركة أولاً
        settings = PayrollSettings.for_company(self.company)
        self.compute(settings)
        super().save(update_fields=["late_minutes", "early_exit_minutes", "worked_hours", "check_in", "check_out"])

    def delete(self, *args, **kwargs):
        self.check_lock()
        return super().delete(*args, **kwargs)


def attendance_summary(employee, start, end):
    records = Attendance.objects.filter(employee=employee, attendance_date__range=(start, end))
    absent = Decimal(0)
    late = early = 0
    overtime = Decimal(0)
    for r in records:
        if r.status == AttendanceStatus.ABSENT:
            absent += 1
        elif r.status == AttendanceStatus.HALF_DAY:
            absent += Decimal("0.5")
        late += r.late_minutes + r.early_exit_minutes
        overtime += r.overtime_hours
    return {"absent_days": absent, "late_minutes": late, "overtime_hours": overtime}


# ───────────────────────────── الرواتب ─────────────────────────────

class EOSWageBasis(models.TextChoices):
    GROSS = "gross", "الأجر الفعلي (الأساسي وجميع البدلات)"
    BASIC_HOUSING = "basic_housing", "الأساسي وبدل السكن"
    BASIC = "basic", "الأساسي فقط"


class Weekend(models.TextChoices):
    FRI_SAT = "fri_sat", "الجمعة والسبت"
    FRI = "fri", "الجمعة فقط"
    SAT_SUN = "sat_sun", "السبت والأحد"
    NONE = "none", "بدون"


# أرقام أيام الأسبوع في بايثون: الاثنين = 0 ... الأحد = 6
WEEKEND_DAYS = {"fri_sat": {4, 5}, "fri": {4}, "sat_sun": {5, 6}, "none": set()}


def default_accounts(company):
    """حسابات الرواتب الافتراضية من الدليل بأسمائها (فلا يُربط حساب أضافته الشركة بنفس الرقم)."""
    from accounts.models import Account
    from core.setup_data import CHART_OF_ACCOUNTS

    names = {}

    def walk(nodes):
        for number, name, _kind, children in nodes:
            names[number] = name
            if children:
                walk(children)

    walk(CHART_OF_ACCOUNTS)
    result = {}
    for number in ("5201", "5209", "5210", "2202", "2103", "2105", "1102", "1107"):
        qs = Account.all_objects.filter(company=company, is_group=False)
        result[number] = qs.filter(name=names[number]).first()
    return result


class PayrollSettings(TenantModel):
    """إعدادات الرواتب والتأمينات الاجتماعية للشركة (سجل واحد لكل شركة)."""

    salary_expense_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, related_name="+", verbose_name="حساب مصروف الرواتب",
        limit_choices_to={"is_group": False, "root_type": "expense"})
    gosi_expense_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, related_name="+",
        verbose_name="حساب مصروف التأمينات (حصة المنشأة)", limit_choices_to={"is_group": False, "root_type": "expense"})
    salary_payable_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, related_name="+", verbose_name="حساب الرواتب المستحقة",
        limit_choices_to={"is_group": False, "root_type": "liability"})
    gosi_payable_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, related_name="+", verbose_name="حساب التأمينات المستحقة",
        limit_choices_to={"is_group": False, "root_type": "liability"})
    deductions_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="حساب الاستقطاعات الأخرى",
        limit_choices_to={"is_group": False},
        help_text="مثل سلف الموظفين. يُترك فارغاً لتخفيض مصروف الرواتب بقيمة الاستقطاعات (الجزاءات والغياب).")
    loan_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="حساب سلف الموظفين",
        limit_choices_to={"is_group": False, "root_type": "asset"})
    payment_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="حساب صرف الرواتب",
        limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]})
    gosi_employee_rate = models.DecimalField("نسبة حصة الموظف السعودي %", max_digits=5, decimal_places=2,
                                             default=Decimal("9.75"))
    gosi_employer_rate = models.DecimalField("نسبة حصة المنشأة عن السعودي %", max_digits=5, decimal_places=2,
                                             default=Decimal("11.75"))
    gosi_employer_rate_non_saudi = models.DecimalField("نسبة حصة المنشأة عن غير السعودي %", max_digits=5,
                                                       decimal_places=2, default=Decimal("2.00"))
    gosi_max_base = models.DecimalField("الحد الأعلى للأجر الخاضع للتأمينات", max_digits=18, decimal_places=2,
                                        default=Decimal("45000"))
    gosi_include_housing = models.BooleanField("بدل السكن يدخل في الأجر الخاضع للتأمينات", default=True)
    days_per_month = models.PositiveSmallIntegerField("عدد أيام الشهر لحساب أجر اليوم", default=30)

    # مكافأة نهاية الخدمة
    eos_expense_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="حساب مصروف مكافأة نهاية الخدمة", limit_choices_to={"is_group": False, "root_type": "expense"})
    eos_provision_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="حساب مخصص مكافأة نهاية الخدمة", limit_choices_to={"is_group": False, "root_type": "liability"})
    accrue_eos = models.BooleanField("تكوين مخصص نهاية الخدمة شهرياً مع المسير", default=True)
    eos_wage_basis = models.CharField("الأجر المعتمد لحساب المكافأة", max_length=20, choices=EOSWageBasis.choices,
                                      default=EOSWageBasis.GROSS)

    # الحضور والانصراف
    work_start = models.TimeField("بداية الدوام", default=datetime.time(8, 0))
    work_end = models.TimeField("نهاية الدوام", default=datetime.time(17, 0))
    hours_per_day = models.DecimalField("ساعات العمل اليومية", max_digits=4, decimal_places=2, default=8)
    grace_minutes = models.PositiveSmallIntegerField("دقائق السماح للتأخير", default=15)
    weekend = models.CharField("أيام الراحة الأسبوعية", max_length=10, choices=Weekend.choices, default=Weekend.FRI_SAT)
    deduct_absence = models.BooleanField("خصم أيام الغياب من الراتب", default=True)
    deduct_late = models.BooleanField("خصم دقائق التأخير من الراتب", default=False)
    overtime_premium = models.DecimalField(
        "علاوة العمل الإضافي % من أجر ساعة الأساسي", max_digits=5, decimal_places=2, default=50,
        help_text="نظام العمل (المادة 107): أجر الساعة الإضافية = أجر الساعة الفعلي + 50% من أجر ساعة الأساسي")

    class Meta:
        verbose_name = "إعدادات الرواتب"
        verbose_name_plural = "إعدادات الرواتب"
        constraints = [tenant_unique(model_label="payrollsettings")]

    def __str__(self):
        return "إعدادات الرواتب"

    @classmethod
    def for_company(cls, company):
        settings = cls.all_objects.filter(company=company).first()
        if settings is None:
            accounts = default_accounts(company)
            settings = cls.all_objects.create(
                company=company,
                eos_expense_account=accounts.get("5210"), eos_provision_account=accounts.get("2202"),
                loan_account=accounts.get("1107"),
                salary_expense_account=accounts.get("5201"), gosi_expense_account=accounts.get("5209"),
                salary_payable_account=accounts.get("2103"), gosi_payable_account=accounts.get("2105"),
                payment_account=accounts.get("1102"),
            )
        return settings

    def validate_accounts(self):
        missing = [self._meta.get_field(f).verbose_name for f in (
            "salary_expense_account", "gosi_expense_account", "salary_payable_account", "gosi_payable_account",
        ) if getattr(self, f"{f}_id") is None]
        if missing:
            raise ValidationError("حدد الحسابات التالية في «الموارد البشرية ← إعدادات الرواتب»: " + "، ".join(missing))

    def validate_loan_account(self):
        if not self.loan_account_id:
            raise ValidationError("حدد «حساب سلف الموظفين» في «إعدادات الرواتب والتأمينات».")

    def validate_eos_accounts(self):
        if not (self.eos_expense_account_id and self.eos_provision_account_id):
            raise ValidationError("حدد حسابي مصروف ومخصص مكافأة نهاية الخدمة في «إعدادات الرواتب والتأمينات».")

    @property
    def weekend_days(self):
        return WEEKEND_DAYS[self.weekend]

    def daily_wage(self, employee):
        return flt(employee.gross_salary) / Decimal(self.days_per_month or 30)

    def schedule(self, employee, date=None):
        """(بداية الدوام، نهايته، دقائق السماح) للموظف في تاريخ حسب ورديته أو الإعدادات العامة."""
        shift = shift_on(employee, date) if date else employee.shift
        if shift is not None:
            return shift.start_time, shift.end_time, shift.grace_minutes
        return self.work_start, self.work_end, self.grace_minutes

    def hours_for(self, employee):
        return Decimal((employee.shift.hours_per_day if employee.shift_id else self.hours_per_day) or 8)

    def hourly_wage(self, employee):
        return self.daily_wage(employee) / self.hours_for(employee)

    def overtime_hour_rate(self, employee):
        basic_hour = flt(employee.basic_salary) / Decimal(self.days_per_month or 30) / self.hours_for(employee)
        return self.hourly_wage(employee) + basic_hour * self.overtime_premium / 100

    def eos_wage(self, employee):
        if self.eos_wage_basis == EOSWageBasis.BASIC:
            return flt(employee.basic_salary)
        if self.eos_wage_basis == EOSWageBasis.BASIC_HOUSING:
            return flt(employee.basic_salary + employee.housing_allowance)
        return flt(employee.gross_salary)


MONTHS = [(1, "يناير"), (2, "فبراير"), (3, "مارس"), (4, "أبريل"), (5, "مايو"), (6, "يونيو"),
          (7, "يوليو"), (8, "أغسطس"), (9, "سبتمبر"), (10, "أكتوبر"), (11, "نوفمبر"), (12, "ديسمبر")]


def _current_year():
    return datetime.date.today().year


def _current_month():
    return datetime.date.today().month


class PayrollRun(SubmittableDocument):
    naming_prefix = "PAYROLL"

    year = models.PositiveIntegerField("السنة", default=_current_year)
    month = models.PositiveSmallIntegerField("الشهر", choices=MONTHS, default=_current_month)
    department = models.ForeignKey(Department, models.PROTECT, null=True, blank=True, verbose_name="القسم",
                                   help_text="اختياري: لإعداد مسير لقسم واحد. يُترك فارغاً لكل الموظفين")
    total_earnings = models.DecimalField("إجمالي الاستحقاقات", max_digits=18, decimal_places=2, default=0, editable=False)
    total_deductions = models.DecimalField("إجمالي الاستقطاعات", max_digits=18, decimal_places=2, default=0, editable=False)
    total_gosi_employer = models.DecimalField("التأمينات - حصة المنشأة", max_digits=18, decimal_places=2, default=0,
                                              editable=False)
    total_eos_accrual = models.DecimalField("مخصص نهاية الخدمة", max_digits=18, decimal_places=2, default=0,
                                            editable=False)
    total_net = models.DecimalField("صافي الرواتب", max_digits=18, decimal_places=2, default=0, editable=False)
    salary_payment = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True, related_name="+",
                                       editable=False, verbose_name="قيد صرف الرواتب")
    gosi_payment = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True, related_name="+",
                                     editable=False, verbose_name="قيد سداد التأمينات")

    class Meta(SubmittableDocument.Meta):
        verbose_name = "مسير رواتب"
        verbose_name_plural = "مسيرات الرواتب"

    @property
    def period_label(self):
        return f"{dict(MONTHS)[self.month]} {self.year}"

    @property
    def period(self):
        return month_bounds(self.year, self.month)

    def clean(self):
        if self.year and not 2000 <= self.year <= 2100:
            raise ValidationError({"year": "سنة غير صحيحة."})

    def eligible_employees(self):
        start, end = self.period
        qs = Employee.objects.filter(date_of_joining__lte=end).exclude(relieving_date__lt=start)
        if self.department_id:
            qs = qs.filter(department=self.department)
        return qs.order_by("employee_number")

    def fetch_employees(self):
        existing = set(self.lines.values_list("employee_id", flat=True))
        PayrollLine.objects.bulk_create([
            PayrollLine(parent=self, employee=emp) for emp in self.eligible_employees() if emp.id not in existing
        ])

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft:
            if not self.lines.exists():
                self.fetch_employees()
            settings = PayrollSettings.for_company(self.company)
            for line in self.lines.select_related("employee"):
                line.compute(settings)
                line.save()
        lines = list(self.lines.all())
        self.total_earnings = flt(sum(line.gross_pay for line in lines))
        self.total_deductions = flt(sum(line.total_deduction for line in lines))
        self.total_gosi_employer = flt(sum(line.gosi_employer for line in lines))
        self.total_eos_accrual = flt(sum(line.eos_accrual for line in lines))
        self.total_net = flt(sum(line.net_pay for line in lines))

    def validate_for_submit(self):
        self.clean()
        lines = list(self.lines.select_related("employee"))
        if not lines:
            raise ValidationError("لا يوجد موظفون في المسير.")
        start, end = self.period
        seen = set()
        for line in lines:
            if line.employee_id in seen:
                raise ValidationError(f"الموظف {line.employee} مكرر في المسير.")
            seen.add(line.employee_id)
            if line.employee.employed_between(start, end) is None:
                raise ValidationError(f"الموظف {line.employee} لم يكن على رأس العمل في {self.period_label}.")
            if line.net_pay < 0:
                raise ValidationError(f"صافي راتب {line.employee} بالسالب ({line.net_pay}). راجع الاستقطاعات.")
            paid = PayrollLine.objects.filter(
                employee=line.employee, parent__year=self.year, parent__month=self.month,
                parent__docstatus=DocStatus.SUBMITTED,
            ).exclude(parent=self).select_related("parent").first()
            if paid:
                raise ValidationError(f"راتب {line.employee} لشهر {self.period_label} مسجل في المسير المعتمد {paid.parent}.")
        if not self.total_earnings and not self.total_gosi_employer:
            raise ValidationError("لا يمكن اعتماد مسير بقيمة صفرية.")
        settings = PayrollSettings.for_company(self.company)
        settings.validate_accounts()
        if self.total_eos_accrual:
            settings.validate_eos_accounts()
        if settings.accrue_eos:
            # المخصص تراكمي لكل موظف، لذا تُعتمد المسيرات بالترتيب الزمني
            later = PayrollLine.objects.filter(
                employee_id__in=seen, parent__docstatus=DocStatus.SUBMITTED,
            ).filter(models.Q(parent__year__gt=self.year) | models.Q(parent__year=self.year, parent__month__gt=self.month)
                     ).select_related("parent", "employee").first()
            if later:
                raise ValidationError(f"يوجد مسير معتمد لشهر لاحق ({later.parent}) للموظف {later.employee}. "
                                      "المسيرات تُعتمد بالترتيب الزمني لأن مخصص نهاية الخدمة تراكمي.")

    def on_submit(self):
        from accounts.ledger import make_gl_entries

        settings = PayrollSettings.for_company(self.company)
        lines = list(self.lines.all())
        expense = sum(line.gross_pay - line.attendance_deduction for line in lines)
        other_deductions = sum(line.other_deductions for line in lines)
        gosi_employee = sum(line.gosi_employee for line in lines)
        loans = sum(line.loan_deduction for line in lines)
        if loans:
            settings.validate_loan_account()
        label = f"رواتب {self.period_label}"
        make_gl_entries(self, [
            {"account": settings.salary_expense_account, "debit": expense, "remarks": label},
            {"account": settings.deductions_account or settings.salary_expense_account, "credit": other_deductions,
             "remarks": f"استقطاعات {label}"},
            {"account": settings.gosi_expense_account, "debit": self.total_gosi_employer,
             "remarks": f"حصة المنشأة في التأمينات - {self.period_label}"},
            {"account": settings.gosi_payable_account, "credit": gosi_employee + self.total_gosi_employer,
             "remarks": f"التأمينات الاجتماعية المستحقة - {self.period_label}"},
            {"account": settings.salary_payable_account, "credit": self.total_net, "remarks": f"صافي {label}"},
        ] + ([{"account": settings.loan_account, "credit": loans, "remarks": f"أقساط سلف الموظفين - {self.period_label}"}]
             if loans else []) + ([
            {"account": settings.eos_expense_account, "debit": self.total_eos_accrual,
             "remarks": f"مخصص مكافأة نهاية الخدمة - {self.period_label}"},
            {"account": settings.eos_provision_account, "credit": self.total_eos_accrual,
             "remarks": f"مخصص مكافأة نهاية الخدمة - {self.period_label}"},
        ] if self.total_eos_accrual else []), remarks=label)
        for line in lines:
            if line.loan_deduction:
                record_loan_repayments(line.employee, self.year, self.month, line.loan_deduction, payroll_line=line)

    def before_cancel(self):
        for je in (self.salary_payment, self.gosi_payment):
            if je is not None and je.docstatus == DocStatus.SUBMITTED:
                raise ValidationError(f"ألغِ قيد الصرف {je} أولاً قبل إلغاء المسير.")

    def on_cancel(self):
        from accounts.ledger import cancel_gl_entries

        cancel_gl_entries(self)
        reverse_loan_repayments(LoanRepayment.objects.filter(payroll_line__parent=self))

    # ── إجراءات بعد الاعتماد ──
    @property
    def total_gosi(self):
        return flt(sum(line.gosi_employee for line in self.lines.all()) + self.total_gosi_employer)

    @property
    def total_cost(self):
        """التكلفة على المنشأة: الاستحقاقات بعد خصم الغياب + حصة المنشأة في التأمينات + مخصص نهاية الخدمة."""
        deducted = sum(line.attendance_deduction for line in self.lines.all())
        return flt(self.total_earnings - deducted + self.total_gosi_employer + self.total_eos_accrual)

    @staticmethod
    def _open(je):
        return je is not None and je.docstatus != DocStatus.CANCELLED

    @property
    def available_actions(self):
        if not self.is_submitted:
            return []
        actions = []
        if not self._open(self.salary_payment) and self.total_net:
            actions.append(("make_salary_payment", "قيد صرف الرواتب"))
        if not self._open(self.gosi_payment) and self.total_gosi:
            actions.append(("make_gosi_payment", "قيد سداد التأمينات"))
        return actions

    def _payment_entry(self, user, payable, amount, title):
        from accounts.models import Account, AccountType, JournalEntry, JournalEntryType

        settings = PayrollSettings.for_company(self.company)
        settings.validate_accounts()
        bank = settings.payment_account or Account.objects.filter(
            is_group=False, disabled=False, account_type=AccountType.BANK).first() or self.company.default_cash_account
        je = JournalEntry.objects.create(entry_type=JournalEntryType.JOURNAL, created_by=user,
                                         remarks=f"{title} - {self.period_label} ({self.name})")
        je.accounts.create(account=payable, debit=amount, description=title)
        je.accounts.create(account=bank, credit=amount, description=title)
        je.calculate_totals()
        je.save()
        return je

    def make_salary_payment(self, user):
        if ("make_salary_payment", "قيد صرف الرواتب") not in self.available_actions:
            raise ValidationError("تم إنشاء قيد صرف الرواتب لهذا المسير مسبقاً.")
        settings = PayrollSettings.for_company(self.company)
        self.salary_payment = self._payment_entry(user, settings.salary_payable_account, self.total_net, "صرف الرواتب")
        self.save(update_fields=["salary_payment"])
        return self.salary_payment

    def make_gosi_payment(self, user):
        if ("make_gosi_payment", "قيد سداد التأمينات") not in self.available_actions:
            raise ValidationError("تم إنشاء قيد سداد التأمينات لهذا المسير مسبقاً.")
        settings = PayrollSettings.for_company(self.company)
        self.gosi_payment = self._payment_entry(user, settings.gosi_payable_account, self.total_gosi,
                                                "سداد التأمينات الاجتماعية")
        self.save(update_fields=["gosi_payment"])
        return self.gosi_payment

    @property
    def payment_entries(self):
        return [je for je in (self.salary_payment, self.gosi_payment) if je is not None]

    def payslips_url(self):
        return reverse("payslips", args=[self.pk])


def eos_provision_balance(employee, exclude_run=None):
    """رصيد مخصص نهاية الخدمة المكوّن للموظف: الرصيد الافتتاحي + ما رُحّل مع المسيرات المعتمدة."""
    lines = PayrollLine.objects.filter(employee=employee, parent__docstatus=DocStatus.SUBMITTED)
    if exclude_run is not None and exclude_run.pk:
        lines = lines.exclude(parent=exclude_run)
    return flt(employee.eos_opening_provision + (lines.aggregate(t=models.Sum("eos_accrual"))["t"] or 0))


class PayrollLine(models.Model):
    parent = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="lines")
    employee = models.ForeignKey(Employee, models.PROTECT, related_name="payroll_lines", verbose_name="الموظف")
    additions = models.DecimalField("إضافات (عمل إضافي/مكافآت)", max_digits=18, decimal_places=2, default=0)
    other_deductions = models.DecimalField("استقطاعات (سلف/جزاءات)", max_digits=18, decimal_places=2, default=0)
    notes = models.CharField("ملاحظات", max_length=255, blank=True)

    worked_days = models.PositiveSmallIntegerField("أيام العمل", default=0, editable=False)
    basic = models.DecimalField("الأساسي", max_digits=18, decimal_places=2, default=0, editable=False)
    housing = models.DecimalField("السكن", max_digits=18, decimal_places=2, default=0, editable=False)
    transport = models.DecimalField("النقل", max_digits=18, decimal_places=2, default=0, editable=False)
    other_allowance = models.DecimalField("بدلات أخرى", max_digits=18, decimal_places=2, default=0, editable=False)
    gross_pay = models.DecimalField("إجمالي الاستحقاق", max_digits=18, decimal_places=2, default=0, editable=False)
    unpaid_days = models.PositiveSmallIntegerField("أيام بدون راتب", default=0, editable=False)
    unpaid_deduction = models.DecimalField("خصم الإجازة بدون راتب", max_digits=18, decimal_places=2, default=0,
                                           editable=False)
    absent_days = models.DecimalField("أيام الغياب", max_digits=5, decimal_places=1, default=0, editable=False)
    absence_deduction = models.DecimalField("خصم الغياب", max_digits=18, decimal_places=2, default=0, editable=False)
    late_minutes = models.PositiveIntegerField("دقائق التأخير", default=0, editable=False)
    late_deduction = models.DecimalField("خصم التأخير", max_digits=18, decimal_places=2, default=0, editable=False)
    overtime_hours = models.DecimalField("ساعات إضافية", max_digits=6, decimal_places=2, default=0, editable=False)
    overtime_pay = models.DecimalField("أجر العمل الإضافي", max_digits=18, decimal_places=2, default=0, editable=False)
    loan_deduction = models.DecimalField("قسط السلفة", max_digits=18, decimal_places=2, default=0, editable=False)
    eos_accrual = models.DecimalField("مخصص نهاية الخدمة للشهر", max_digits=18, decimal_places=2, default=0,
                                      editable=False)
    gosi_employee = models.DecimalField("التأمينات (حصة الموظف)", max_digits=18, decimal_places=2, default=0,
                                        editable=False)
    gosi_employer = models.DecimalField("التأمينات (حصة المنشأة)", max_digits=18, decimal_places=2, default=0,
                                        editable=False)
    total_deduction = models.DecimalField("إجمالي الاستقطاع", max_digits=18, decimal_places=2, default=0,
                                          editable=False)
    net_pay = models.DecimalField("صافي الراتب", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "راتب موظف"
        verbose_name_plural = "رواتب الموظفين"
        ordering = ["employee__employee_number"]

    def __str__(self):
        return f"{self.employee} - {self.parent.period_label}"

    def compute(self, settings):
        emp = self.employee
        start, end = self.parent.period
        month_days = (end - start).days + 1
        window = emp.employed_between(start, end)
        worked = (window[1] - window[0]).days + 1 if window else 0
        # الشهر الكامل يُحسب كاملاً؛ الشهر الجزئي (تعيين أو انتهاء خدمة) بنسبة الأيام
        factor = Decimal(1) if worked == month_days else Decimal(worked) / Decimal(month_days)
        self.worked_days = worked
        self.basic = flt(emp.basic_salary * factor)
        self.housing = flt(emp.housing_allowance * factor)
        self.transport = flt(emp.transport_allowance * factor)
        self.other_allowance = flt(emp.other_allowance * factor)
        earned = self.basic + self.housing + self.transport + self.other_allowance
        daily = settings.daily_wage(emp)

        summary = attendance_summary(emp, *window) if window else {
            "absent_days": Decimal(0), "late_minutes": 0, "overtime_hours": Decimal(0)}
        self.absent_days = summary["absent_days"]
        self.late_minutes = summary["late_minutes"]
        self.overtime_hours = summary["overtime_hours"]
        self.overtime_pay = flt(settings.overtime_hour_rate(emp) * self.overtime_hours)
        self.gross_pay = flt(earned + self.overtime_pay + flt(self.additions))

        self.unpaid_days = unpaid_leave_days(emp, *window) if window else 0
        self.unpaid_deduction = min(flt(daily * self.unpaid_days), earned)
        remaining = earned - self.unpaid_deduction
        self.absence_deduction = min(flt(daily * self.absent_days), remaining) if settings.deduct_absence else flt(0)
        remaining -= self.absence_deduction
        self.late_deduction = (min(flt(settings.hourly_wage(emp) * self.late_minutes / 60), remaining)
                               if settings.deduct_late else flt(0))
        self.eos_accrual = self.compute_eos_accrual(settings, window)

        self.gosi_employee = self.gosi_employer = flt(0)
        if emp.gosi_subscribed and worked:
            wage = emp.basic_salary + (emp.housing_allowance if settings.gosi_include_housing else 0)
            base = min(flt(wage), flt(settings.gosi_max_base))
            if emp.is_saudi:
                self.gosi_employee = flt(base * settings.gosi_employee_rate / 100)
                self.gosi_employer = flt(base * settings.gosi_employer_rate / 100)
            else:
                self.gosi_employer = flt(base * settings.gosi_employer_rate_non_saudi / 100)

        deductions = flt(self.unpaid_deduction + self.absence_deduction + self.late_deduction
                         + flt(self.other_deductions) + self.gosi_employee)
        # أقساط السلف تُخصم من المتاح فقط حتى لا يصبح الصافي سالباً
        available = max(self.gross_pay - deductions, flt(0))
        self.loan_deduction = flt(0)
        for loan, amount in loan_installments(emp, self.parent.year, self.parent.month, available):
            self.loan_deduction += amount
        self.total_deduction = flt(deductions + self.loan_deduction)
        self.net_pay = flt(self.gross_pay - self.total_deduction)

    @property
    def attendance_deduction(self):
        return self.unpaid_deduction + self.absence_deduction + self.late_deduction

    def compute_eos_accrual(self, settings, window):
        """المخصص = الالتزام الكامل في نهاية الشهر بالأجر الحالي − ما كُوّن للموظف سابقاً (تسوية تلقائية لتغيّر الأجر)."""
        from hr.eos import calculate

        emp = self.employee
        if not settings.accrue_eos or window is None or emp.settlements.filter(docstatus=DocStatus.SUBMITTED).exists():
            return flt(0)
        liability = calculate(emp.date_of_joining, window[1], settings.eos_wage(emp)).award
        return flt(liability - eos_provision_balance(emp, exclude_run=self.parent))


# ───────────────────────────── مكافأة نهاية الخدمة ─────────────────────────────

class EndOfServiceSettlement(SubmittableDocument):
    """المخالصة النهائية: مكافأة نهاية الخدمة + بدل رصيد الإجازات + الإضافات − الاستقطاعات."""

    naming_prefix = "EOS"

    employee = models.ForeignKey(Employee, models.PROTECT, related_name="settlements", verbose_name="الموظف")
    last_working_date = models.DateField("آخر يوم عمل")
    reason = models.CharField("سبب انتهاء الخدمة", max_length=20, choices=SeparationReason.choices,
                              default=SeparationReason.CONTRACT_END)
    leave_days_override = models.DecimalField(
        "أيام الإجازة المستحقة للصرف", max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="يُترك فارغاً لحسابه تلقائياً من رصيد السنة الأخيرة؛ أدخله يدوياً لإضافة أرصدة مرحّلة من سنوات سابقة")
    additions = models.DecimalField("إضافات (رواتب متأخرة/مستحقات أخرى)", max_digits=18, decimal_places=2, default=0)
    other_deductions = models.DecimalField("استقطاعات أخرى (عهد/جزاءات)", max_digits=18, decimal_places=2, default=0)
    loan_deduction = models.DecimalField("رصيد السلف المستردة", max_digits=18, decimal_places=2, default=0,
                                         editable=False)

    service_years = models.DecimalField("مدة الخدمة (سنة)", max_digits=8, decimal_places=4, default=0, editable=False)
    wage = models.DecimalField("الأجر المعتمد للحساب", max_digits=18, decimal_places=2, default=0, editable=False)
    full_award = models.DecimalField("المكافأة الكاملة", max_digits=18, decimal_places=2, default=0, editable=False)
    entitlement_percent = models.DecimalField("نسبة الاستحقاق %", max_digits=6, decimal_places=2, default=0,
                                              editable=False)
    award = models.DecimalField("مكافأة نهاية الخدمة المستحقة", max_digits=18, decimal_places=2, default=0,
                                editable=False)
    leave_days = models.DecimalField("أيام الإجازة المصروفة", max_digits=6, decimal_places=2, default=0, editable=False)
    leave_encashment = models.DecimalField("بدل رصيد الإجازات", max_digits=18, decimal_places=2, default=0,
                                           editable=False)
    provision_balance = models.DecimalField("المخصص المكوّن للموظف", max_digits=18, decimal_places=2, default=0,
                                            editable=False)
    net_amount = models.DecimalField("صافي المستحق للموظف", max_digits=18, decimal_places=2, default=0, editable=False)
    previous_status = models.CharField(max_length=10, blank=True, editable=False)
    previous_relieving_date = models.DateField(null=True, blank=True, editable=False)
    payment = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True, related_name="+",
                                editable=False, verbose_name="قيد صرف المستحقات")

    class Meta(SubmittableDocument.Meta):
        verbose_name = "مخالصة نهاية خدمة"
        verbose_name_plural = "مخالصات نهاية الخدمة"

    @property
    def provision_difference(self):
        return flt(self.award - self.provision_balance)

    @property
    def service_duration(self):
        from hr.eos import describe_duration

        return describe_duration(self.employee.date_of_joining, self.last_working_date)

    def clean(self):
        if self.employee_id and self.last_working_date and self.last_working_date < self.employee.date_of_joining:
            raise ValidationError({"last_working_date": "آخر يوم عمل قبل تاريخ التعيين."})

    def suggested_leave_days(self):
        """رصيد الإجازات القابلة للصرف في سنة انتهاء الخدمة، مستحقاً بنسبة أيام العمل فيها."""
        end = self.last_working_date
        start = max(datetime.date(end.year, 1, 1), self.employee.date_of_joining)
        year_days = Decimal(366 if calendar.isleap(end.year) else 365)
        total = Decimal(0)
        for lt in LeaveType.objects.filter(encashable=True, days_per_year__gt=0):
            earned = Decimal(lt.days_per_year) * Decimal((end - start).days + 1) / year_days
            total += max(earned - used_leave_days(self.employee, lt, end.year), Decimal(0))
        return flt(total)

    def calculate_totals(self):
        from hr.eos import calculate

        if not (self.employee_id and self.last_working_date) or self.last_working_date < self.employee.date_of_joining:
            return
        settings = PayrollSettings.for_company(self.company)
        result = calculate(self.employee.date_of_joining, self.last_working_date, settings.eos_wage(self.employee),
                           self.reason)
        self.service_years = result.years.quantize(Decimal("0.0001"))
        self.wage, self.full_award, self.award = result.wage, result.full, result.award
        self.entitlement_percent = flt(result.ratio * 100)
        self.leave_days = flt(self.leave_days_override if self.leave_days_override is not None
                              else self.suggested_leave_days())
        self.leave_encashment = flt(settings.daily_wage(self.employee) * self.leave_days)
        if self.is_draft:
            self.provision_balance = eos_provision_balance(self.employee)
            self.loan_deduction = flt(sum(loan.outstanding for loan in open_loans(self.employee)))
        self.net_amount = flt(self.award + self.leave_encashment + flt(self.additions) - flt(self.other_deductions)
                              - self.loan_deduction)

    def validate_for_submit(self):
        self.clean()
        if self.net_amount < 0:
            raise ValidationError("صافي المستحق بالسالب؛ راجع الاستقطاعات.")
        other = EndOfServiceSettlement.objects.filter(employee=self.employee, docstatus=DocStatus.SUBMITTED
                                                      ).exclude(pk=self.pk).first()
        if other:
            raise ValidationError(f"للموظف مخالصة معتمدة سابقاً ({other}).")
        later = PayrollLine.objects.filter(employee=self.employee, parent__docstatus=DocStatus.SUBMITTED).filter(
            models.Q(parent__year__gt=self.last_working_date.year)
            | models.Q(parent__year=self.last_working_date.year, parent__month__gt=self.last_working_date.month)
        ).select_related("parent").first()
        if later:
            raise ValidationError(f"صُرف للموظف راتب بعد آخر يوم عمل في المسير {later.parent}.")
        settings = PayrollSettings.for_company(self.company)
        settings.validate_accounts()
        if self.award or self.provision_balance:
            settings.validate_eos_accounts()

    def on_submit(self):
        from accounts.ledger import make_gl_entries

        settings = PayrollSettings.for_company(self.company)
        label = f"مخالصة نهاية خدمة {self.employee.full_name}"
        if self.loan_deduction:
            settings.validate_loan_account()
        entries = [e for e in [
            # المخصص المكوّن يُقفل بالكامل؛ الفرق عن المكافأة الفعلية مصروف إضافي (أو رد للمخصص الزائد)
            {"account": settings.eos_provision_account, "debit": self.provision_balance, "remarks": "إقفال المخصص"},
            {"account": settings.eos_expense_account, "debit": self.award - self.provision_balance,
             "remarks": "فرق المكافأة عن المخصص"},
            {"account": settings.salary_expense_account, "debit": self.leave_encashment + flt(self.additions),
             "remarks": "بدل رصيد الإجازات والمستحقات"},
            {"account": settings.deductions_account or settings.salary_expense_account,
             "credit": self.other_deductions, "remarks": "استقطاعات"},
            {"account": settings.loan_account, "credit": self.loan_deduction, "remarks": "استرداد رصيد السلف"},
            {"account": settings.salary_payable_account, "credit": self.net_amount, "remarks": "صافي المستحق"},
        ] if e.get("debit") or e.get("credit")]
        make_gl_entries(self, entries, remarks=label)
        for loan in open_loans(self.employee):
            LoanRepayment.objects.create(loan=loan, settlement=self, amount=loan.outstanding)
            loan.refresh_repaid()
        employee = self.employee
        self.previous_status, self.previous_relieving_date = employee.status, employee.relieving_date
        self.save(update_fields=["previous_status", "previous_relieving_date"])
        employee.status, employee.relieving_date = EmployeeStatus.LEFT, self.last_working_date
        employee.save(update_fields=["status", "relieving_date"])

    def before_cancel(self):
        if self.payment is not None and self.payment.docstatus == DocStatus.SUBMITTED:
            raise ValidationError(f"ألغِ قيد الصرف {self.payment} أولاً.")

    def on_cancel(self):
        from accounts.ledger import cancel_gl_entries

        cancel_gl_entries(self)
        reverse_loan_repayments(self.loan_repayments.all())
        employee = self.employee
        employee.status = self.previous_status or EmployeeStatus.ACTIVE
        employee.relieving_date = self.previous_relieving_date
        employee.save(update_fields=["status", "relieving_date"])

    @property
    def available_actions(self):
        if self.is_submitted and self.net_amount and (self.payment is None or self.payment.docstatus == DocStatus.CANCELLED):
            return [("make_payment", "قيد صرف المستحقات")]
        return []

    def make_payment(self, user):
        from accounts.models import Account, AccountType, JournalEntry

        if not self.available_actions:
            raise ValidationError("تم إنشاء قيد الصرف مسبقاً.")
        settings = PayrollSettings.for_company(self.company)
        bank = settings.payment_account or Account.objects.filter(
            is_group=False, disabled=False, account_type=AccountType.BANK).first() or self.company.default_cash_account
        je = JournalEntry.objects.create(created_by=user, remarks=f"صرف مستحقات نهاية الخدمة - {self.employee} ({self.name})")
        je.accounts.create(account=settings.salary_payable_account, debit=self.net_amount, description="مستحقات نهاية الخدمة")
        je.accounts.create(account=bank, credit=self.net_amount, description="مستحقات نهاية الخدمة")
        je.calculate_totals()
        je.save()
        self.payment = je
        self.save(update_fields=["payment"])
        return je



# ───────────────────────────── سلف الموظفين ─────────────────────────────

class EmployeeLoan(PortalRequestMixin, SubmittableDocument):
    """سلفة تُصرف للموظف وتُسترد بأقساط شهرية تُخصم تلقائياً في المسير."""

    naming_prefix = "LOAN"

    employee = models.ForeignKey(Employee, models.PROTECT, related_name="loans", verbose_name="الموظف")
    amount = models.DecimalField("مبلغ السلفة", max_digits=18, decimal_places=2)
    installment_amount = models.DecimalField("القسط الشهري", max_digits=18, decimal_places=2)
    start_year = models.PositiveIntegerField("سنة أول قسط", default=_current_year)
    start_month = models.PositiveSmallIntegerField("شهر أول قسط", choices=MONTHS, default=_current_month)
    payment_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="صُرفت من حساب",
        limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]},
        help_text="يُترك فارغاً لاستخدام حساب صرف الرواتب")
    repaid_amount = models.DecimalField("المسترد", max_digits=18, decimal_places=2, default=0, editable=False)
    requested_via_portal = models.BooleanField("طلب من بوابة الموظف", default=False, editable=False)
    rejection_reason = models.CharField("سبب الرفض", max_length=255, blank=True, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "سلفة موظف"
        verbose_name_plural = "سلف الموظفين"

    @property
    def outstanding(self):
        return flt(self.amount - self.repaid_amount)

    @property
    def request_label(self):
        return f"طلب سلفة بمبلغ {self.amount:,.2f}"

    @property
    def installments_count(self):
        if not self.installment_amount:
            return 0
        return math.ceil(Decimal(str(self.amount)) / Decimal(str(self.installment_amount)))

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "مبلغ السلفة يجب أن يكون أكبر من صفر."})
        if self.installment_amount is not None and self.amount is not None and not (
                0 < self.installment_amount <= self.amount):
            raise ValidationError({"installment_amount": "القسط يجب أن يكون أكبر من صفر ولا يتجاوز مبلغ السلفة."})

    def validate_for_submit(self):
        self.clean()
        if self.employee.status == EmployeeStatus.LEFT:
            raise ValidationError("لا تُصرف سلفة لموظف منتهية خدماته.")
        settings = PayrollSettings.for_company(self.company)
        settings.validate_loan_account()
        if self.payment_account_id is None:
            from accounts.models import Account, AccountType

            self.payment_account = settings.payment_account or Account.objects.filter(
                is_group=False, disabled=False, account_type=AccountType.BANK).first()
            if self.payment_account is None:
                raise ValidationError("حدد الحساب الذي صُرفت منه السلفة.")

    def on_submit(self):
        from accounts.ledger import make_gl_entries

        settings = PayrollSettings.for_company(self.company)
        label = f"سلفة {self.employee.full_name}"
        make_gl_entries(self, [
            {"account": settings.loan_account, "debit": self.amount},
            {"account": self.payment_account, "credit": self.amount},
        ], remarks=label)
        self.notify_employee(f"اعتُمد {self.request_label}، ويبدأ الخصم {self.get_start_month_display()} {self.start_year}")

    def before_cancel(self):
        if self.repayments.exists():
            raise ValidationError("بدأ استرداد هذه السلفة في مسيرات معتمدة؛ لا يمكن إلغاؤها.")

    def on_cancel(self):
        from accounts.ledger import cancel_gl_entries

        cancel_gl_entries(self)

    def refresh_repaid(self):
        self.repaid_amount = flt(self.repayments.aggregate(t=models.Sum("amount"))["t"] or 0)
        self.save(update_fields=["repaid_amount"])


class LoanRepayment(models.Model):
    loan = models.ForeignKey(EmployeeLoan, models.CASCADE, related_name="repayments", verbose_name="السلفة")
    payroll_line = models.ForeignKey(PayrollLine, models.CASCADE, null=True, blank=True, related_name="loan_repayments")
    settlement = models.ForeignKey(EndOfServiceSettlement, models.CASCADE, null=True, blank=True,
                                   related_name="loan_repayments")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)

    class Meta:
        verbose_name = "قسط مسترد"
        verbose_name_plural = "الأقساط المستردة"


def open_loans(employee):
    return [loan for loan in EmployeeLoan.objects.filter(employee=employee, docstatus=DocStatus.SUBMITTED)
            .order_by("start_year", "start_month", "id") if loan.outstanding > 0]


def loan_installments(employee, year, month, available):
    """الأقساط المستحقة في الشهر: [(السلفة، المبلغ)] بحد أقصى المتاح من الراتب."""
    result = []
    for loan in open_loans(employee):
        if (loan.start_year, loan.start_month) > (year, month) or available <= 0:
            continue
        amount = min(loan.installment_amount, loan.outstanding, available)
        result.append((loan, flt(amount)))
        available -= amount
    return result


def record_loan_repayments(employee, year, month, total, payroll_line):
    for loan, amount in loan_installments(employee, year, month, total):
        LoanRepayment.objects.create(loan=loan, payroll_line=payroll_line, amount=amount)
        loan.refresh_repaid()


def reverse_loan_repayments(repayments):
    loans = {r.loan for r in repayments.select_related("loan")}
    repayments.delete()
    for loan in loans:
        loan.refresh_repaid()


# ───────────────────────────── تقييم الأداء ─────────────────────────────

DEFAULT_APPRAISAL_CRITERIA = [
    ("جودة العمل ودقته", 25),
    ("الإنتاجية وإنجاز المهام في وقتها", 25),
    ("الالتزام بالدوام والأنظمة", 15),
    ("العمل الجماعي والتعاون", 15),
    ("المبادرة والتطوير الذاتي", 10),
    ("التعامل مع العملاء والزملاء", 10),
]

RATINGS = [(90, "ممتاز"), (80, "جيد جداً"), (70, "جيد"), (60, "مقبول"), (0, "ضعيف")]


class AppraisalTemplate(TenantModel):
    name = models.CharField("اسم النموذج", max_length=100)

    class Meta:
        verbose_name = "نموذج تقييم"
        verbose_name_plural = "نماذج تقييم الأداء"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="appraisaltemplate")]

    def __str__(self):
        return self.name


class AppraisalCriterion(models.Model):
    parent = models.ForeignKey(AppraisalTemplate, models.CASCADE, related_name="criteria")
    name = models.CharField("المعيار", max_length=150)
    weight = models.PositiveSmallIntegerField("الوزن %", default=10)

    class Meta:
        verbose_name = "معيار تقييم"
        verbose_name_plural = "معايير التقييم"
        ordering = ["id"]

    def __str__(self):
        return self.name


class Appraisal(SubmittableDocument):
    """تقييم أداء موظف عن فترة: درجة من 1 إلى 5 لكل معيار، والنتيجة الموزونة بالنسبة المئوية."""

    naming_prefix = "APR"
    posts_to_ledger = False
    posting_note = "التقييم لا يُنشئ قيوداً محاسبية؛ بعد اعتماده يظهر للموظف في بوابته."

    employee = models.ForeignKey(Employee, models.PROTECT, related_name="appraisals", verbose_name="الموظف")
    template = models.ForeignKey(AppraisalTemplate, models.PROTECT, verbose_name="نموذج التقييم")
    period_from = models.DateField("الفترة من")
    period_to = models.DateField("الفترة إلى")
    strengths = models.TextField("نقاط القوة", blank=True)
    improvements = models.TextField("فرص التحسين والأهداف القادمة", blank=True)
    score_percent = models.DecimalField("النتيجة %", max_digits=5, decimal_places=1, default=0, editable=False)
    rating = models.CharField("التقدير", max_length=20, blank=True, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "تقييم أداء"
        verbose_name_plural = "تقييمات الأداء"

    def clean(self):
        if self.period_from and self.period_to and self.period_to < self.period_from:
            raise ValidationError({"period_to": "نهاية الفترة يجب أن تكون بعد بدايتها."})

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft and not self.scores.exists() and self.template_id:
            AppraisalScore.objects.bulk_create([
                AppraisalScore(parent=self, criterion=c.name, weight=c.weight) for c in self.template.criteria.all()])
        scores = [s for s in self.scores.all() if s.score]
        weights = sum(s.weight for s in scores)
        if weights:
            self.score_percent = (Decimal(sum(s.weight * s.score for s in scores)) / weights / 5 * 100).quantize(
                Decimal("0.1"))
        else:
            self.score_percent = Decimal(0)
        self.rating = next(label for limit, label in RATINGS if self.score_percent >= limit) if weights else ""

    def validate_for_submit(self):
        self.clean()
        scores = list(self.scores.all())
        if not scores:
            raise ValidationError("لا توجد معايير في التقييم.")
        missing = [s.criterion for s in scores if not s.score]
        if missing:
            raise ValidationError("أدخل درجة لكل معيار: " + "، ".join(missing))
        if sum(s.weight for s in scores) != 100:
            raise ValidationError(f"مجموع أوزان المعايير يجب أن يساوي 100 (الحالي {sum(s.weight for s in scores)}).")

    def on_submit(self):
        from core.notify import notify

        if self.employee.user_id:
            notify(self.company, [self.employee.user], f"صدر تقييم أدائك ({self.rating} — {self.score_percent:f}%)",
                   reverse("portal_home"))


class AppraisalScore(models.Model):
    parent = models.ForeignKey(Appraisal, models.CASCADE, related_name="scores")
    criterion = models.CharField("المعيار", max_length=150)
    weight = models.PositiveSmallIntegerField("الوزن %", default=10)
    score = models.PositiveSmallIntegerField("الدرجة (1-5)", null=True, blank=True,
                                             choices=[(i, str(i)) for i in range(1, 6)])
    comment = models.CharField("ملاحظة", max_length=255, blank=True)

    class Meta:
        verbose_name = "درجة معيار"
        verbose_name_plural = "درجات المعايير"
        ordering = ["id"]
