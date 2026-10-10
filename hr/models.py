import calendar
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

from core.models import DocStatus, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


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

    class Meta:
        verbose_name = "موظف"
        verbose_name_plural = "الموظفون"
        ordering = ["employee_number"]
        constraints = [tenant_unique("employee_number", model_label="employee")]

    def __str__(self):
        return f"{self.employee_number} - {self.full_name}"

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
    return sum(overlap_days(start, end, lv.from_date, lv.to_date) for lv in leaves)


def unpaid_leave_days(employee, start, end):
    """أيام الإجازات غير المدفوعة المعتمدة الواقعة داخل الفترة."""
    leaves = LeaveApplication.objects.filter(
        employee=employee, leave_type__is_paid=False, docstatus=DocStatus.SUBMITTED,
        from_date__lte=end, to_date__gte=start,
    )
    return sum(overlap_days(start, end, lv.from_date, lv.to_date) for lv in leaves)


class LeaveApplication(SubmittableDocument):
    naming_prefix = "LV"
    posts_to_ledger = False

    employee = models.ForeignKey(Employee, models.PROTECT, related_name="leaves", verbose_name="الموظف")
    leave_type = models.ForeignKey(LeaveType, models.PROTECT, verbose_name="نوع الإجازة",
                                   limit_choices_to={"disabled": False})
    from_date = models.DateField("من تاريخ")
    to_date = models.DateField("إلى تاريخ")
    total_days = models.PositiveIntegerField("عدد الأيام", default=0, editable=False)

    posting_note = "طلب الإجازة لا يُنشئ قيوداً محاسبية؛ أيام الإجازة بدون راتب تُخصم تلقائياً في مسير رواتب الشهر."

    class Meta(SubmittableDocument.Meta):
        verbose_name = "طلب إجازة"
        verbose_name_plural = "طلبات الإجازات"

    def clean(self):
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValidationError({"to_date": "تاريخ النهاية يجب أن يكون بعد تاريخ البداية أو يساويه."})

    def calculate_totals(self):
        if self.from_date and self.to_date and self.to_date >= self.from_date:
            self.total_days = (self.to_date - self.from_date).days + 1

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
                requested = overlap_days(start, end, self.from_date, self.to_date)
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


# ───────────────────────────── الرواتب ─────────────────────────────

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
            from accounts.models import Account

            accounts = {a.number: a for a in Account.all_objects.filter(
                company=company, is_group=False, number__in=["5201", "5209", "2103", "2105", "1102"])}
            settings = cls.all_objects.create(
                company=company,
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
        if not self.total_net and not self.total_gosi_employer:
            raise ValidationError("لا يمكن اعتماد مسير بقيمة صفرية.")
        PayrollSettings.for_company(self.company).validate_accounts()

    def on_submit(self):
        from accounts.ledger import make_gl_entries

        settings = PayrollSettings.for_company(self.company)
        lines = list(self.lines.all())
        expense = sum(line.gross_pay - line.unpaid_deduction for line in lines)
        other_deductions = sum(line.other_deductions for line in lines)
        gosi_employee = sum(line.gosi_employee for line in lines)
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
        ], remarks=label)

    def before_cancel(self):
        for je in (self.salary_payment, self.gosi_payment):
            if je is not None and je.docstatus == DocStatus.SUBMITTED:
                raise ValidationError(f"ألغِ قيد الصرف {je} أولاً قبل إلغاء المسير.")

    def on_cancel(self):
        from accounts.ledger import cancel_gl_entries

        cancel_gl_entries(self)

    # ── إجراءات بعد الاعتماد ──
    @property
    def total_gosi(self):
        return flt(sum(line.gosi_employee for line in self.lines.all()) + self.total_gosi_employer)

    @property
    def total_cost(self):
        """التكلفة على المنشأة: الاستحقاقات بعد خصم الغياب غير المدفوع + حصة المنشأة في التأمينات."""
        unpaid = sum(line.unpaid_deduction for line in self.lines.all())
        return flt(self.total_earnings - unpaid + self.total_gosi_employer)

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
        self.gross_pay = flt(self.basic + self.housing + self.transport + self.other_allowance + flt(self.additions))

        self.unpaid_days = unpaid_leave_days(emp, *window) if window else 0
        daily = flt(emp.gross_salary) / Decimal(settings.days_per_month or 30)
        earned = self.basic + self.housing + self.transport + self.other_allowance
        self.unpaid_deduction = min(flt(daily * self.unpaid_days), earned)

        self.gosi_employee = self.gosi_employer = flt(0)
        if emp.gosi_subscribed and worked:
            wage = emp.basic_salary + (emp.housing_allowance if settings.gosi_include_housing else 0)
            base = min(flt(wage), flt(settings.gosi_max_base))
            if emp.is_saudi:
                self.gosi_employee = flt(base * settings.gosi_employee_rate / 100)
                self.gosi_employer = flt(base * settings.gosi_employer_rate / 100)
            else:
                self.gosi_employer = flt(base * settings.gosi_employer_rate_non_saudi / 100)

        self.total_deduction = flt(self.unpaid_deduction + flt(self.other_deductions) + self.gosi_employee)
        self.net_pay = flt(self.gross_pay - self.total_deduction)
