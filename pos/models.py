from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count, F, Sum
from django.utils import timezone

from core.models import DocStatus, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class TerminalMode(models.TextChoices):
    MANUAL = "manual", "يدوي: الكاشير يُدخل رقم الموافقة من جهاز مدى"
    BRIDGE = "bridge", "ربط مباشر عبر برنامج الوسيط على جهاز الكاشير"
    SIMULATOR = "simulator", "تجريبي للتدريب (لا يقبض مالاً حقيقياً)"


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
    price_list = models.ForeignKey("pricing.PriceList", models.SET_NULL, null=True, blank=True, related_name="+",
                                   limit_choices_to={"kind": "selling", "disabled": False},
                                   verbose_name="قائمة الأسعار", help_text="فارغة = أسعار بطاقات الأصناف (أو قائمة العميل)")
    apply_pricing_rules = models.BooleanField("تطبيق العروض وقواعد التسعير", default=True)
    receipt_footer = models.CharField("تذييل الإيصال", max_length=255, blank=True, default="شكراً لزيارتكم")
    card_terminal_mode = models.CharField("ربط جهاز الدفع (مدى)", max_length=10, choices=TerminalMode.choices,
                                          default=TerminalMode.MANUAL)
    bridge_url = models.CharField("عنوان برنامج الوسيط", max_length=200, blank=True, default="http://127.0.0.1:8099",
                                  help_text="للربط المباشر: البرنامج الوسيط على جهاز الكاشير الذي يتصل بجهاز مدى")
    terminal_id = models.CharField("رقم جهاز الدفع (TID)", max_length=30, blank=True)
    require_card_reference = models.BooleanField(
        "إلزام رقم الموافقة في الوضع اليدوي", default=True,
        help_text="يطلب من الكاشير إدخال رقم الموافقة المطبوع على إيصال جهاز مدى")
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
        # مطابقة البطاقات: ما سجّلته الفواتير مقابل عمليات الجهاز الموثقة (يُطابق بعدها مع تسوية البنك)
        tx = self.card_transactions.filter(invoice__docstatus=DocStatus.SUBMITTED).aggregate(
            count=Count("id"), total=Sum("amount"))
        data["card_tx_count"] = tx["count"]
        data["card_tx_total"] = flt(tx["total"])
        data["card_unmatched"] = flt(data["card"] - data["card_tx_total"])
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


class CardTransaction(TenantModel):
    """عملية بطاقة (مدى/فيزا/ماستركارد) مرتبطة بفاتورة نقطة البيع — للتدقيق ومطابقة تسويات البنك.

    لا يُخزن رقم البطاقة كاملاً أبداً؛ آخر 4 أرقام فقط.
    """

    shift = models.ForeignKey(POSShift, models.PROTECT, related_name="card_transactions", verbose_name="الوردية")
    invoice = models.ForeignKey("selling.SalesInvoice", models.PROTECT, null=True, blank=True,
                                related_name="card_transactions", verbose_name="الفاتورة")
    kind = models.CharField("النوع", max_length=10, choices=[("sale", "بيع"), ("refund", "استرداد")], default="sale")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    mode = models.CharField("الوضع", max_length=10, choices=TerminalMode.choices)
    auth_code = models.CharField("رقم الموافقة", max_length=20, blank=True)
    rrn = models.CharField("الرقم المرجعي (RRN)", max_length=30, blank=True)
    scheme = models.CharField("نوع البطاقة", max_length=20, blank=True)
    card_last4 = models.CharField("آخر 4 أرقام", max_length=4, blank=True)
    terminal_id = models.CharField("رقم الجهاز (TID)", max_length=30, blank=True)
    created_at = models.DateTimeField("الوقت", auto_now_add=True)

    class Meta:
        verbose_name = "عملية بطاقة"
        verbose_name_plural = "عمليات البطاقات"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} {self.auth_code or ''}"


def clean_card_info(info, mode, amount, require_reference=True):
    """تنقية بيانات البطاقة الواردة من الشاشة أو البرنامج الوسيط، ورفض ما لا يصح."""
    import re
    from decimal import Decimal, InvalidOperation

    from django.core.exceptions import ValidationError

    info = info or {}
    data = {
        "auth_code": re.sub(r"[^0-9A-Za-z]", "", str(info.get("auth_code") or ""))[:20],
        "rrn": re.sub(r"[^0-9A-Za-z]", "", str(info.get("rrn") or ""))[:30],
        "scheme": re.sub(r"[^A-Za-z ]", "", str(info.get("scheme") or ""))[:20].strip().lower(),
        # من رقم البطاقة المقنّع نأخذ آخر 4 أرقام فقط مهما أُرسل
        "card_last4": re.sub(r"[^0-9]", "", str(info.get("masked_pan") or info.get("card_last4") or ""))[-4:],
        "terminal_id": re.sub(r"[^0-9A-Za-z-]", "", str(info.get("terminal_id") or ""))[:30],
    }
    status = str(info.get("status") or "approved").lower()
    if mode in (TerminalMode.BRIDGE, TerminalMode.SIMULATOR):
        if status != "approved":
            raise ValidationError("لم يوافق جهاز الدفع على العملية.")
        if not (data["auth_code"] or data["rrn"]):
            raise ValidationError("لم تصل موافقة جهاز الدفع (رقم الموافقة أو المرجع).")
        if str(info.get("mode") or mode) != mode:
            raise ValidationError("وضع الدفع لا يطابق إعداد نقطة البيع.")
        approved = info.get("amount")
        if approved not in (None, ""):
            try:
                approved = abs(Decimal(str(approved)))
            except (InvalidOperation, ValueError):
                raise ValidationError("مبلغ عملية جهاز الدفع غير صالح.")
            if abs(approved - abs(Decimal(str(amount)))) > Decimal("0.01"):
                raise ValidationError(f"المبلغ الذي وافق عليه جهاز الدفع ({approved}) لا يطابق مبلغ البطاقة "
                                      f"في الفاتورة ({abs(Decimal(str(amount)))}).")
    elif require_reference and not data["auth_code"]:
        raise ValidationError("أدخل رقم الموافقة المطبوع على إيصال جهاز مدى.")
    return data
