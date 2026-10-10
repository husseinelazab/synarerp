from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count, F, Sum
from django.utils import timezone

from core.models import DocStatus, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class POSProfile(TenantModel):
    """إعدادات نقطة البيع: المستودع، الحسابات، العميل الافتراضي، والصلاحيات."""

    name = models.CharField("اسم نقطة البيع", max_length=100)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, verbose_name="المستودع")
    default_customer = models.ForeignKey("selling.Customer", models.PROTECT, verbose_name="العميل الافتراضي")
    cash_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="حساب النقدية",
        limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]},
    )
    card_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="حساب مدفوعات البطاقات (مدى/فيزا)",
        limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]},
    )
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True,
                                    limit_choices_to={"is_group": False, "disabled": False},
                                    verbose_name="مركز التكلفة / الفرع")
    tax_rate = models.DecimalField("نسبة الضريبة %", max_digits=5, decimal_places=2, default=15)
    prices_include_tax = models.BooleanField("الأسعار شاملة الضريبة", default=True)
    allow_discount = models.BooleanField("السماح بالخصم", default=True)
    max_discount_percent = models.DecimalField("أقصى نسبة خصم %", max_digits=5, decimal_places=2, default=10)
    allow_rate_change = models.BooleanField("السماح بتعديل السعر", default=False)
    receipt_footer = models.CharField("تذييل الإيصال", max_length=255, blank=True, default="شكراً لزيارتكم")
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "ملف نقطة بيع"
        verbose_name_plural = "ملفات نقاط البيع"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="posprofile")]

    def __str__(self):
        return self.name


class ShiftStatus(models.TextChoices):
    OPEN = "open", "مفتوحة"
    CLOSED = "closed", "مغلقة"


class POSShift(TenantModel):
    """وردية الكاشير: تبدأ برصيد افتتاحي وتنتهي بجرد النقدية ومطابقتها."""

    profile = models.ForeignKey(POSProfile, models.PROTECT, related_name="shifts", verbose_name="نقطة البيع")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.PROTECT, related_name="pos_shifts", verbose_name="الكاشير")
    status = models.CharField("الحالة", max_length=10, choices=ShiftStatus.choices, default=ShiftStatus.OPEN)
    opened_at = models.DateTimeField("وقت الفتح", default=timezone.now)
    closed_at = models.DateTimeField("وقت الإغلاق", null=True, blank=True)
    opening_cash = models.DecimalField("النقدية الافتتاحية", max_digits=18, decimal_places=2, default=0)
    expected_cash = models.DecimalField("النقدية المتوقعة", max_digits=18, decimal_places=2, default=0)
    closing_cash = models.DecimalField("النقدية الفعلية عند الإغلاق", max_digits=18, decimal_places=2, default=0)
    difference = models.DecimalField("الفرق (عجز − / زيادة +)", max_digits=18, decimal_places=2, default=0)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "وردية نقطة بيع"
        verbose_name_plural = "ورديات نقاط البيع"
        ordering = ["-opened_at"]

    def __str__(self):
        return f"وردية #{self.pk} - {self.user} - {self.profile}"

    @property
    def is_open(self):
        return self.status == ShiftStatus.OPEN

    @classmethod
    def current_for(cls, user):
        return cls.objects.filter(user=user, status=ShiftStatus.OPEN).select_related("profile").first()

    @classmethod
    def open(cls, profile, user, opening_cash=0):
        if cls.current_for(user):
            raise ValidationError("لديك وردية مفتوحة بالفعل.")
        if profile.disabled:
            raise ValidationError("نقطة البيع موقوفة.")
        if flt(opening_cash) < 0:
            raise ValidationError("النقدية الافتتاحية لا يمكن أن تكون سالبة.")
        return cls.objects.create(company=profile.company, profile=profile, user=user, opening_cash=flt(opening_cash))

    def summary(self):
        invoices = self.invoices.filter(docstatus=DocStatus.SUBMITTED)
        agg = invoices.aggregate(
            count=Count("id"), grand=Sum("grand_total"), tax=Sum("tax_amount"), net=Sum("net_total"),
            discount=Sum("discount_amount"), cash=Sum(F("paid_cash") - F("change_amount")), card=Sum("paid_card"),
        )
        data = {k: flt(v) if k != "count" else v for k, v in agg.items()}
        returns = invoices.filter(is_return=True).aggregate(count=Count("id"), total=Sum("grand_total"))
        data["returns_count"] = returns["count"]
        data["returns_total"] = -flt(returns["total"])
        data["count"] -= returns["count"]
        data["cancelled"] = self.invoices.filter(docstatus=DocStatus.CANCELLED).count()
        data["expected_cash"] = flt(self.opening_cash + data["cash"])
        return data

    def close(self, closing_cash, notes=""):
        if not self.is_open:
            raise ValidationError("الوردية مغلقة بالفعل.")
        if flt(closing_cash) < 0:
            raise ValidationError("النقدية الفعلية لا يمكن أن تكون سالبة.")
        self.expected_cash = self.summary()["expected_cash"]
        self.closing_cash = flt(closing_cash)
        self.difference = self.closing_cash - self.expected_cash
        self.notes = notes
        self.status = ShiftStatus.CLOSED
        self.closed_at = timezone.now()
        self.save()
