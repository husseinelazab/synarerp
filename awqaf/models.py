"""إدارة الأوقاف: الوقف وشرط الواقف (مصارف الغلة)، عقود الإيجار وأقساطها، قبض الريع، وتوزيع الغلة على المصارف.

أصل الوقف في صندوق «وقف» لا يُصرف منه، والغلة في صندوق مقيد، والتوزيع إعادة تصنيف من صندوق الغلة إلى صناديق المصارف
بحسابي التحويلات وإعادة التصنيف في الدليل الموحد (47101001 ← 31401001)؛ ثم يُصرف من كل مصرف بمستنداته (مثل صرف المساعدات).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from accounts.ledger import cancel_gl_entries, make_gl_entries
from accounts.models import Account, Fund, FundType
from core.models import SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt

RENT_ACCOUNT = "31303001"          # ريع أوقاف
TRANSFER_OUT_ACCOUNT = "47101001"  # مصروفات التحويلات وإعادة التصنيف - قيود استخدام
TRANSFER_IN_ACCOUNT = "31401001"   # إيرادات التحويلات وإعادة التصنيف - قيود استخدام


def account_by_number(number):
    account = Account.objects.filter(number=number, is_group=False).first()
    if account is None:
        raise ValidationError(f"الحساب {number} غير موجود في دليل الحسابات (الدليل الموحد للجهات غير الربحية).")
    return account


def add_months(date, months):
    month = date.month - 1 + months
    year, month = date.year + month // 12, month % 12 + 1
    for day in (date.day, 30, 29, 28):
        try:
            return date.replace(year=year, month=month, day=day)
        except ValueError:
            continue


class WaqfType(models.TextChoices):
    REAL_ESTATE = "real_estate", "عقاري"
    CASH = "cash", "نقدي"
    INVESTMENT = "investment", "استثماري"
    MOVABLE = "movable", "منقول"


class RevenueRestriction(models.TextChoices):
    UNRESTRICTED = "unrestricted", "غير مقيدة (أعمال البر وأغراض الجمعية عامة)"
    RESTRICTED = "restricted", "مقيدة بشرط الواقف (مصارف محددة)"


class Waqf(TenantModel):
    name = models.CharField("اسم الوقف", max_length=150)
    waqf_type = models.CharField("نوع الوقف", max_length=12, choices=WaqfType.choices, default=WaqfType.REAL_ESTATE)
    waqif = models.CharField("الواقف", max_length=150, blank=True)
    deed_number = models.CharField("رقم صك الوقفية", max_length=50, blank=True)
    deed_date = models.DateField("تاريخ الصك", null=True, blank=True)
    location = models.CharField("الموقع / العنوان", max_length=255, blank=True)
    asset_value = models.DecimalField("قيمة الأصل الموقوف", max_digits=18, decimal_places=2, default=0)
    conditions = models.TextField("شرط الواقف", blank=True)
    revenue_restriction = models.CharField(
        "تصنيف الغلة", max_length=12, choices=RevenueRestriction.choices, default=RevenueRestriction.UNRESTRICTED,
        help_text="وفق معيار المنشآت غير الهادفة للربح: الغلة غير مقيدة ما لم يشترط الواقف صرفها في مصارف محددة")
    principal_fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="+", editable=False,
                                       verbose_name="صندوق أصل الوقف")
    revenue_fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="+", editable=False,
                                     verbose_name="صندوق الغلة")
    is_active = models.BooleanField("قائم", default=True)

    class Meta:
        verbose_name = "وقف"
        verbose_name_plural = "الأوقاف"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="waqf")]

    def __str__(self):
        return self.name

    def clean(self):
        fund = self.revenue_fund if self.revenue_fund_id else None
        if fund is not None and fund.fund_type != self.revenue_restriction and fund.gl_entries.exists():
            raise ValidationError({"revenue_restriction": "لا يُغيَّر تصنيف الغلة بعد تسجيل حركات على صندوقها؛ "
                                                          "عالج الفرق بقيد إعادة تصنيف."})

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        changed = []
        if self.principal_fund_id is None:
            self.principal_fund = Fund.objects.create(company=self.company, name=f"أصل {self.name}",
                                                      fund_type=FundType.ENDOWMENT, purpose=self.conditions[:500])
            changed.append("principal_fund")
        if self.revenue_fund_id is None:
            self.revenue_fund = Fund.objects.create(company=self.company, name=f"غلة {self.name}",
                                                    fund_type=self.revenue_restriction,
                                                    purpose=f"تُوزع حسب شرط واقف {self.name}")
            changed.append("revenue_fund")
        elif self.revenue_fund.fund_type != self.revenue_restriction:
            self.revenue_fund.fund_type = self.revenue_restriction
            self.revenue_fund.save(update_fields=["fund_type"])
        if changed:
            super().save(update_fields=changed)

    @property
    def shares_total(self):
        return sum((s.percent for s in self.shares.all()), Decimal(0))

    def validate_shares(self):
        if not self.shares.exists():
            raise ValidationError("حدد مصارف الغلة (شرط الواقف) في بطاقة الوقف أولاً.")
        if self.shares_total != 100:
            raise ValidationError(f"مجموع نسب مصارف الغلة يجب أن يساوي 100% (الحالي {self.shares_total}%).")

    @property
    def revenue_available(self):
        return self.revenue_fund.balance if self.revenue_fund_id else flt(0)

    @property
    def principal_balance(self):
        return self.principal_fund.balance if self.principal_fund_id else flt(0)

    def contracted_rent(self):
        return flt(self.leases.filter(status=LeaseStatus.ACTIVE).aggregate(t=models.Sum("annual_rent"))["t"])


class WaqfShare(models.Model):
    """مصرف من مصارف الغلة حسب شرط الواقف، ونسبته، والصندوق الذي تُحوّل إليه حصته."""

    parent = models.ForeignKey(Waqf, models.CASCADE, related_name="shares")
    name = models.CharField("المصرف", max_length=150, help_text="مثل: النظارة، عمارة الوقف، الأيتام، المساجد")
    percent = models.DecimalField("النسبة %", max_digits=5, decimal_places=2)
    target_fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_active": True}, verbose_name="يُحوّل إلى صندوق",
                                    help_text="فارغ = الأموال العامة غير المقيدة (مثل نصيب النظارة)")

    class Meta:
        verbose_name = "مصرف غلة"
        verbose_name_plural = "مصارف الغلة (شرط الواقف)"
        ordering = ["id"]

    def clean(self):
        if self.percent is not None and not (0 < self.percent <= 100):
            raise ValidationError({"percent": "النسبة بين 0 و100."})
        if self.target_fund_id and self.target_fund.fund_type == FundType.ENDOWMENT:
            raise ValidationError({"target_fund": "لا تُحوّل الغلة إلى صندوق وقف؛ اختر صندوقاً مقيداً أو اتركه فارغاً."})
        if (self.target_fund_id and self.target_fund.fund_type == FundType.RESTRICTED and self.parent_id
                and self.parent.revenue_restriction == RevenueRestriction.UNRESTRICTED):
            raise ValidationError({"target_fund": "غلة هذا الوقف غير مقيدة، ولا يُقيدها إلا شرط الواقف؛ "
                                                  "غيّر تصنيف الغلة إن كان الصك يحدد المصارف."})


class LeaseStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    ACTIVE = "active", "ساري"
    ENDED = "ended", "منتهٍ"
    TERMINATED = "terminated", "مفسوخ"


FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}


class WaqfLease(TenantModel):
    waqf = models.ForeignKey(Waqf, models.PROTECT, related_name="leases", verbose_name="الوقف",
                             limit_choices_to={"is_active": True})
    unit = models.CharField("الوحدة المؤجرة", max_length=150, help_text="مثل: شقة 3 الدور الثاني، معرض رقم 1")
    tenant_name = models.CharField("المستأجر", max_length=150)
    tenant_id_number = models.CharField("هوية / سجل المستأجر", max_length=20, blank=True)
    tenant_phone = models.CharField("جوال المستأجر", max_length=20, blank=True)
    contract_number = models.CharField("رقم العقد (إيجار)", max_length=50, blank=True)
    start_date = models.DateField("بداية العقد")
    end_date = models.DateField("نهاية العقد")
    annual_rent = models.DecimalField("الإيجار السنوي", max_digits=14, decimal_places=2)
    frequency = models.CharField("دورية السداد", max_length=12, default="semiannual",
                                 choices=[("monthly", "شهري"), ("quarterly", "ربع سنوي"),
                                          ("semiannual", "نصف سنوي"), ("annual", "سنوي")])
    status = models.CharField("الحالة", max_length=12, choices=LeaseStatus.choices, default=LeaseStatus.DRAFT,
                              editable=False)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "عقد إيجار وقف"
        verbose_name_plural = "عقود إيجار الأوقاف"
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return f"{self.waqf} — {self.unit} ({self.tenant_name})"

    def clean(self):
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": "نهاية العقد يجب أن تكون بعد بدايته."})
        if self.annual_rent is not None and self.annual_rent <= 0:
            raise ValidationError({"annual_rent": "الإيجار يجب أن يكون أكبر من صفر."})

    @property
    def available_actions(self):
        if self.status == LeaseStatus.DRAFT:
            return [("activate", "تفعيل العقد وتوليد الأقساط")]
        if self.status == LeaseStatus.ACTIVE:
            return [("terminate", "فسخ العقد"), ("close", "إنهاء العقد")]
        return []

    def schedule(self):
        step = FREQUENCY_MONTHS[self.frequency]
        amount = flt(self.annual_rent * step / 12)
        rows, due, index = [], self.start_date, 0
        while due < self.end_date:
            rows.append((due, amount))
            index += 1
            due = add_months(self.start_date, step * index)
        return rows

    def activate(self, user):
        self.full_clean()
        overlapping = WaqfLease.objects.filter(waqf=self.waqf, unit=self.unit, status=LeaseStatus.ACTIVE,
                                               start_date__lt=self.end_date, end_date__gt=self.start_date)
        if overlapping.exclude(pk=self.pk).exists():
            raise ValidationError(f"الوحدة «{self.unit}» مؤجرة بعقد ساري في نفس الفترة.")
        RentInstallment.objects.bulk_create([RentInstallment(company=self.company, lease=self, due_date=due, amount=amount)
                                             for due, amount in self.schedule()])
        self.status = LeaseStatus.ACTIVE
        self.save(update_fields=["status"])

    def terminate(self, user):
        self.status = LeaseStatus.TERMINATED
        self.save(update_fields=["status"])
        self.installments.filter(status=InstallmentStatus.PENDING, due_date__gt=timezone.localdate()).update(
            status=InstallmentStatus.CANCELLED)

    def close(self, user):
        self.status = LeaseStatus.ENDED
        self.save(update_fields=["status"])

    @property
    def outstanding(self):
        return flt(sum((i.outstanding for i in self.installments.exclude(status=InstallmentStatus.CANCELLED)),
                       flt(0)))


class InstallmentStatus(models.TextChoices):
    PENDING = "pending", "غير محصّل"
    PARTIAL = "partial", "محصّل جزئياً"
    PAID = "paid", "محصّل"
    CANCELLED = "cancelled", "ملغى"


class RentInstallment(TenantModel):
    lease = models.ForeignKey(WaqfLease, models.CASCADE, related_name="installments", verbose_name="العقد")
    due_date = models.DateField("تاريخ الاستحقاق")
    amount = models.DecimalField("المبلغ", max_digits=14, decimal_places=2)
    collected_amount = models.DecimalField("المحصّل", max_digits=14, decimal_places=2, default=0, editable=False)
    status = models.CharField("الحالة", max_length=10, choices=InstallmentStatus.choices,
                              default=InstallmentStatus.PENDING, editable=False)

    class Meta:
        verbose_name = "قسط إيجار"
        verbose_name_plural = "أقساط الإيجار"
        ordering = ["due_date", "id"]

    def __str__(self):
        return f"{self.lease.unit} — {self.due_date:%Y-%m-%d}"

    @property
    def outstanding(self):
        return flt(max(flt(self.amount) - flt(self.collected_amount), 0))

    @property
    def is_overdue(self):
        return self.status in (InstallmentStatus.PENDING, InstallmentStatus.PARTIAL) and \
            self.due_date < timezone.localdate()

    def refresh_collected(self):
        self.collected_amount = flt(self.receipts.filter(docstatus=1).aggregate(t=models.Sum("amount"))["t"])
        if self.status != InstallmentStatus.CANCELLED:
            self.status = (InstallmentStatus.PAID if self.collected_amount >= self.amount else
                           InstallmentStatus.PARTIAL if self.collected_amount else InstallmentStatus.PENDING)
        self.save(update_fields=["collected_amount", "status"])

    @property
    def available_actions(self):
        if self.status in (InstallmentStatus.PENDING, InstallmentStatus.PARTIAL):
            return [("collect", "تحصيل القسط")]
        return []

    def collect(self, user):
        return RentReceipt.objects.create(installment=self, lease=self.lease, amount=self.outstanding,
                                          account=self.company.default_cash_account, created_by=user)


class RentReceipt(SubmittableDocument):
    """سند قبض ريع وقف: يُقيد في ريع الأوقاف (31303001) ضمن صندوق غلة الوقف."""

    naming_prefix = "RNT"

    lease = models.ForeignKey(WaqfLease, models.PROTECT, related_name="receipts", verbose_name="عقد الإيجار")
    installment = models.ForeignKey(RentInstallment, models.PROTECT, null=True, blank=True, related_name="receipts",
                                    verbose_name="القسط")
    amount = models.DecimalField("المبلغ", max_digits=14, decimal_places=2)
    account = models.ForeignKey(Account, models.PROTECT, related_name="+", verbose_name="حساب الصندوق / البنك",
                                limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]})
    reference_no = models.CharField("رقم المرجع", max_length=50, blank=True)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "سند قبض ريع"
        verbose_name_plural = "سندات قبض الريع"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "المبلغ يجب أن يكون أكبر من صفر."})
        if self.installment_id and self.lease_id and self.installment.lease_id != self.lease_id:
            raise ValidationError({"installment": "القسط من عقد آخر."})

    def validate_for_submit(self):
        self.clean()
        if self.installment_id and self.amount > self.installment.outstanding:
            raise ValidationError(f"المبلغ يتجاوز المتبقي من القسط ({self.installment.outstanding}).")

    def on_submit(self):
        waqf = self.lease.waqf
        make_gl_entries(self, [{"account": self.account, "debit": self.amount},
                               {"account": account_by_number(RENT_ACCOUNT), "credit": self.amount,
                                "fund": waqf.revenue_fund}],
                        remarks=f"ريع {waqf} — {self.lease.unit} ({self.lease.tenant_name})")
        if self.installment_id:
            self.installment.refresh_collected()

    def on_cancel(self):
        cancel_gl_entries(self)
        if self.installment_id:
            self.installment.refresh_collected()


class WaqfDistribution(SubmittableDocument):
    """توزيع غلة الوقف على مصارفها حسب شرط الواقف (إعادة تصنيف بين الصناديق)."""

    naming_prefix = "WQD"

    waqf = models.ForeignKey(Waqf, models.PROTECT, related_name="distributions", verbose_name="الوقف")
    amount = models.DecimalField("المبلغ الموزع", max_digits=18, decimal_places=2, default=0,
                                 help_text="صفر = كامل رصيد صندوق الغلة المتاح")

    class Meta(SubmittableDocument.Meta):
        verbose_name = "توزيع غلة وقف"
        verbose_name_plural = "توزيع غلة الأوقاف"

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft:
            if not self.amount:
                self.amount = max(self.waqf.revenue_available, flt(0))
            self.lines.all().delete()
            DistributionLine.objects.bulk_create([
                DistributionLine(parent=self, share_name=s.name, percent=s.percent, target_fund=s.target_fund,
                                 amount=flt(self.amount * s.percent / 100)) for s in self.waqf.shares.all()])
            lines = list(self.lines.all())
            if lines:  # فرق التقريب على آخر مصرف
                last = lines[-1]
                last.amount = flt(self.amount - sum((l.amount for l in lines[:-1]), flt(0)))
                last.save(update_fields=["amount"])

    def validate_for_submit(self):
        self.waqf.validate_shares()
        if self.amount <= 0:
            raise ValidationError("لا توجد غلة متاحة للتوزيع.")

    def on_submit(self):
        out_account, in_account = account_by_number(TRANSFER_OUT_ACCOUNT), account_by_number(TRANSFER_IN_ACCOUNT)
        entries = [{"account": out_account, "debit": self.amount, "fund": self.waqf.revenue_fund}]
        entries += [{"account": in_account, "credit": line.amount, "fund": line.target_fund,
                     "remarks": f"نصيب {line.share_name} من غلة {self.waqf}"} for line in self.lines.all()]
        make_gl_entries(self, entries, remarks=f"توزيع غلة {self.waqf}")

    def on_cancel(self):
        cancel_gl_entries(self)


class DistributionLine(models.Model):
    parent = models.ForeignKey(WaqfDistribution, models.CASCADE, related_name="lines")
    share_name = models.CharField("المصرف", max_length=150)
    percent = models.DecimalField("النسبة %", max_digits=5, decimal_places=2)
    target_fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="+",
                                    verbose_name="الصندوق")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0)

    class Meta:
        verbose_name = "نصيب مصرف"
        verbose_name_plural = "أنصبة المصارف"
        ordering = ["id"]
