"""المتجر الإلكتروني للتبرعات: فرص التبرع، وطلبات التبرع المدفوعة عبر بوابة ميسّر، والمسوّقون، والتبرع الشهري."""
import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from accounts.models import AccountType, Fund, FundType
from core.models import NamingSeries, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class StoreMode(models.TextChoices):
    TEST = "test", "تجريبي"
    LIVE = "live", "مباشر"


class StoreSettings(TenantModel):
    """إعدادات المتجر وبوابة الدفع (ميسّر) لكل جمعية."""

    enabled = models.BooleanField("تفعيل المتجر", default=False)
    mode = models.CharField("وضع البوابة", max_length=4, choices=StoreMode.choices, default=StoreMode.TEST)
    publishable_key = models.CharField("المفتاح العام (Publishable Key)", max_length=120, blank=True,
                                       help_text="يبدأ بـ pk_test_ أو pk_live_")
    secret_key_encrypted = models.TextField(blank=True, editable=False)
    webhook_secret_encrypted = models.TextField(blank=True, editable=False)
    clearing_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+",
        limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH, AccountType.BANK]},
        verbose_name="حساب التسوية (البنك المستلم من ميسّر)",
        help_text="الحساب الذي تُقيد فيه التبرعات الإلكترونية حتى تُحوَّل من ميسّر")
    suggested_amounts = models.CharField("المبالغ المقترحة", max_length=100, default="50,100,250,500",
                                         help_text="مفصولة بفواصل")
    thank_you_message = models.TextField("رسالة الشكر", blank=True,
                                         default="جزاكم الله خيراً على تبرعكم، تقبّل الله منكم.")
    allow_recurring = models.BooleanField("السماح بالتبرع الشهري", default=True)
    gold_gram_price = models.DecimalField(
        "سعر جرام الذهب (عيار 24)", max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="يُعرض مبدئياً في حاسبة الزكاة لحساب النصاب (85 جراماً)، ويمكن للمتبرع تعديله")

    class Meta:
        verbose_name = "إعدادات المتجر"
        verbose_name_plural = "إعدادات المتجر"
        constraints = [models.UniqueConstraint(fields=["company"], name="store_settings_one_per_company")]

    def __str__(self):
        return f"إعدادات متجر {self.company}"

    @classmethod
    def for_company(cls, company):
        obj = cls.all_objects.filter(company=company).first()
        if obj is None:
            obj = cls(company=company)
        if not obj.clearing_account_id:
            obj.clearing_account = default_clearing_account(company)
        return obj

    # المفاتيح السرية مشفرة ولا تُعرض
    @property
    def secret_key(self):
        from core.mail import decrypt_password

        return decrypt_password(self.secret_key_encrypted)

    @secret_key.setter
    def secret_key(self, value):
        from core.mail import encrypt_password

        self.secret_key_encrypted = encrypt_password(value)

    @property
    def webhook_secret(self):
        from core.mail import decrypt_password

        return decrypt_password(self.webhook_secret_encrypted)

    @webhook_secret.setter
    def webhook_secret(self, value):
        from core.mail import encrypt_password

        self.webhook_secret_encrypted = encrypt_password(value)

    @property
    def ready(self):
        return bool(self.enabled and self.publishable_key and self.secret_key_encrypted and self.clearing_account_id)

    @property
    def amounts(self):
        result = []
        for part in (self.suggested_amounts or "").replace("،", ",").split(","):
            try:
                value = int(part.strip())
            except ValueError:
                continue
            if value > 0:
                result.append(value)
        return result or [50, 100, 250, 500]


def default_clearing_account(company):
    from accounts.models import Account

    bank = Account.all_objects.filter(company=company, is_group=False, account_type=AccountType.BANK).order_by(
        "number").first()
    return bank or company.default_cash_account


class OpportunityKind(models.TextChoices):
    GENERAL = "general", "عام"
    ZAKAT = "zakat", "زكاة"
    PROJECT = "project", "مشروع"
    CASE = "case", "حالة"
    SPONSORSHIP = "sponsorship", "كفالة"
    WAQF = "waqf", "سهم وقفي"
    RESTRICTED = "restricted", "مقيد بغرض"


# نوع سند التبرع لكل نوع فرصة
DONATION_TYPES = {"general": "general", "zakat": "zakat", "project": "restricted", "case": "restricted",
                  "restricted": "restricted", "sponsorship": "sponsorship", "waqf": "endowment"}


class DonationOpportunity(TenantModel):
    title = models.CharField("العنوان", max_length=150)
    kind = models.CharField("النوع", max_length=12, choices=OpportunityKind.choices, default=OpportunityKind.GENERAL)
    description = models.TextField("الوصف", blank=True)
    image = models.TextField("الصورة", blank=True, editable=False)  # data URI
    target_amount = models.DecimalField("المبلغ المستهدف", max_digits=14, decimal_places=2, null=True, blank=True)
    fixed_amount = models.DecimalField("قيمة السهم / الوحدة", max_digits=12, decimal_places=2, null=True, blank=True,
                                       help_text="مثل سهم وقفي بـ 100 ريال: يختار المتبرع عدد الأسهم")
    allow_custom_amount = models.BooleanField("السماح بمبلغ يحدده المتبرع", default=True)
    fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="+",
                             limit_choices_to={"is_active": True}, verbose_name="الصندوق",
                             help_text="إلزامي للمشروع والحالة والمقيد بغرض والكفالة. الزكاة تُقيد تلقائياً في صندوق "
                                       "الزكاة، والسهم الوقفي في صندوق أصل الوقف")
    waqf = models.ForeignKey("awqaf.Waqf", models.PROTECT, null=True, blank=True, related_name="+",
                             verbose_name="الوقف", help_text="للسهم الوقفي")
    sponsorship_type = models.ForeignKey("funds.SponsorshipType", models.PROTECT, null=True, blank=True,
                                         related_name="+", verbose_name="نوع الكفالة")
    project = models.ForeignKey("projects.Project", models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="المشروع")
    is_published = models.BooleanField("منشورة", default=True)
    is_featured = models.BooleanField("مميزة في الصفحة الرئيسية", default=False)
    sort_order = models.PositiveIntegerField("الترتيب", default=0)
    end_date = models.DateField("تاريخ الانتهاء", null=True, blank=True)

    class Meta:
        verbose_name = "فرصة تبرع"
        verbose_name_plural = "فرص التبرع"
        ordering = ["sort_order", "-id"]

    def __str__(self):
        return self.title

    @property
    def donation_type(self):
        return DONATION_TYPES[self.kind]

    def resolve_fund(self):
        """الصندوق الذي يُقيد فيه التبرع."""
        if self.kind == OpportunityKind.ZAKAT:
            return Fund.all_objects.filter(company_id=self.company_id, name="صندوق الزكاة", is_active=True).first()
        if self.kind == OpportunityKind.WAQF and self.waqf_id:
            return self.waqf.principal_fund
        if self.kind == OpportunityKind.SPONSORSHIP and not self.fund_id and self.sponsorship_type_id:
            return self.sponsorship_type.aid_program.fund
        return self.fund if self.fund_id else None

    def clean(self):
        from funds.models import REQUIRED_FUND_TYPE

        if self.kind == OpportunityKind.WAQF and not self.waqf_id:
            raise ValidationError({"waqf": "حدد الوقف للسهم الوقفي."})
        if self.kind == OpportunityKind.ZAKAT:
            self.fund = Fund.objects.filter(name="صندوق الزكاة", is_active=True).first()
            if self.fund is None:
                raise ValidationError({"fund": "لا يوجد «صندوق الزكاة» نشط في الصناديق."})
        if self.kind == OpportunityKind.WAQF:
            self.fund = self.waqf.principal_fund
        if self.kind == OpportunityKind.SPONSORSHIP and not self.fund_id and self.sponsorship_type_id:
            self.fund = self.sponsorship_type.aid_program.fund
        required = REQUIRED_FUND_TYPE.get(self.donation_type)
        fund = self.fund if self.fund_id else None
        label = self.get_kind_display()
        if required and fund is None:
            raise ValidationError({"fund": f"حدد الصندوق لفرصة «{label}»."})
        if required and fund.fund_type != required:
            raise ValidationError({"fund": f"«{label}» يُقيد في صندوق {FundType(required).label}، "
                                           f"و«{fund}» {fund.get_fund_type_display()}."})
        if not required and fund is not None and fund.fund_type != FundType.UNRESTRICTED:
            raise ValidationError({"fund": "التبرع العام لا يُقيد في صندوق مقيد. اختر «مقيد بغرض» أو اترك الصندوق."})
        if self.fixed_amount is not None and self.fixed_amount <= 0:
            raise ValidationError({"fixed_amount": "قيمة السهم يجب أن تكون أكبر من صفر."})
        if self.target_amount is not None and self.target_amount <= 0:
            raise ValidationError({"target_amount": "المبلغ المستهدف يجب أن يكون أكبر من صفر."})

    @property
    def is_open(self):
        return self.is_published and (self.end_date is None or self.end_date >= timezone.localdate()) and not (
            self.target_amount and self.raised >= self.target_amount)

    @property
    def raised(self):
        if not hasattr(self, "_raised"):
            self._raised = flt(DonationOrderLine.objects.filter(
                opportunity=self, order__status=OrderStatus.PAID).aggregate(t=models.Sum("amount"))["t"])
        return self._raised

    @property
    def percent(self):
        if not self.target_amount:
            return 0
        return min(int(self.raised * 100 / flt(self.target_amount)), 100)

    @property
    def remaining(self):
        return flt(max(flt(self.target_amount) - self.raised, 0)) if self.target_amount else None

    @property
    def is_monthly_default(self):
        return self.kind == OpportunityKind.SPONSORSHIP


class Marketer(TenantModel):
    name = models.CharField("الاسم", max_length=150)
    code = models.SlugField("رمز الإحالة", max_length=40, help_text="أحرف إنجليزية وأرقام وشرطة، يُضاف للرابط ?ref=الرمز")
    phone = models.CharField("الجوال", max_length=20, blank=True)
    is_active = models.BooleanField("نشط", default=True)

    class Meta:
        verbose_name = "مسوّق / سفير"
        verbose_name_plural = "المسوّقون والسفراء"
        ordering = ["name"]
        constraints = [tenant_unique("code", model_label="marketer")]

    def __str__(self):
        return self.name

    def clean(self):
        self.code = (self.code or "").strip().lower()

    @property
    def paid_orders(self):
        return self.orders.filter(status=OrderStatus.PAID)

    @property
    def total_raised(self):
        return flt(self.paid_orders.aggregate(t=models.Sum("total"))["t"])


class OrderStatus(models.TextChoices):
    PENDING = "pending", "بانتظار الدفع"
    PAID = "paid", "مدفوع"
    FAILED = "failed", "فشل"
    REFUNDED = "refunded", "مسترد"


def _reference():
    return secrets.token_hex(8)


def _verify_code():
    return secrets.token_hex(4).upper()


class DonationOrder(TenantModel):
    number = models.CharField("رقم الطلب", max_length=30, editable=False)
    reference = models.CharField("المرجع", max_length=16, unique=True, default=_reference, editable=False)
    verify_code = models.CharField("رمز التحقق", max_length=8, default=_verify_code, editable=False)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    donor_name = models.CharField("اسم المتبرع", max_length=150)
    donor_phone = models.CharField("جوال المتبرع", max_length=20)
    donor_email = models.EmailField("البريد الإلكتروني", blank=True)
    is_anonymous = models.BooleanField("فاعل خير (إخفاء الاسم)", default=False)
    donor = models.ForeignKey("funds.Donor", models.SET_NULL, null=True, blank=True, related_name="+",
                              editable=False, verbose_name="الداعم")
    gift_to_name = models.CharField("إهداء إلى", max_length=150, blank=True)
    gift_to_phone = models.CharField("جوال المُهدى إليه", max_length=20, blank=True)
    gift_message = models.CharField("رسالة الإهداء", max_length=300, blank=True)
    marketer = models.ForeignKey(Marketer, models.SET_NULL, null=True, blank=True, related_name="orders",
                                 verbose_name="المسوّق")
    total = models.DecimalField("الإجمالي", max_digits=14, decimal_places=2, default=0)
    status = models.CharField("الحالة", max_length=10, choices=OrderStatus.choices, default=OrderStatus.PENDING)
    is_monthly = models.BooleanField("تبرع شهري", default=False)
    payment_id = models.CharField("رقم عملية الدفع", max_length=64, blank=True, db_index=True)
    payment_method = models.CharField("وسيلة الدفع", max_length=40, blank=True)
    paid_at = models.DateTimeField("تاريخ الدفع", null=True, blank=True)
    failure_reason = models.CharField("سبب الفشل", max_length=255, blank=True)
    gateway_response = models.JSONField("استجابة البوابة", default=dict, blank=True, editable=False)
    recurring = models.ForeignKey("store.RecurringDonation", models.SET_NULL, null=True, blank=True,
                                  related_name="orders", verbose_name="التبرع الشهري")

    class Meta:
        verbose_name = "طلب تبرع إلكتروني"
        verbose_name_plural = "طلبات التبرع الإلكتروني"
        ordering = ["-id"]
        constraints = [tenant_unique("number", model_label="donationorder")]

    def __str__(self):
        return self.number

    def save(self, *args, **kwargs):
        if not self.number:
            from core.models import Company

            self.number = NamingSeries.next_name(self.company if self.company_id else Company.current(), "ORD")
        super().save(*args, **kwargs)

    @property
    def is_paid(self):
        return self.status == OrderStatus.PAID

    @property
    def display_name(self):
        return "فاعل خير" if self.is_anonymous else self.donor_name

    @property
    def amount_halalas(self):
        return int(round(flt(self.total) * 100))

    @property
    def description(self):
        titles = [line.opportunity.title for line in self.lines.all()]
        return f"تبرع {self.number}: " + "، ".join(titles)[:200]

    def receipts(self):
        from funds.models import DonationReceipt

        return DonationReceipt.objects.filter(store_lines__order=self).distinct()


class DonationOrderLine(models.Model):
    order = models.ForeignKey(DonationOrder, models.CASCADE, related_name="lines", verbose_name="الطلب")
    opportunity = models.ForeignKey(DonationOpportunity, models.PROTECT, related_name="order_lines",
                                    verbose_name="فرصة التبرع")
    quantity = models.PositiveIntegerField("الكمية", default=1)
    amount = models.DecimalField("المبلغ", max_digits=14, decimal_places=2)
    receipt = models.ForeignKey("funds.DonationReceipt", models.SET_NULL, null=True, blank=True,
                                related_name="store_lines", verbose_name="سند التبرع")

    class Meta:
        verbose_name = "بند طلب تبرع"
        verbose_name_plural = "بنود طلب التبرع"

    def __str__(self):
        return f"{self.opportunity} × {self.quantity}"


class RecurringDonation(TenantModel):
    donor_name = models.CharField("اسم المتبرع", max_length=150)
    donor_phone = models.CharField("جوال المتبرع", max_length=20)
    opportunity = models.ForeignKey(DonationOpportunity, models.PROTECT, related_name="recurring",
                                    verbose_name="فرصة التبرع")
    amount = models.DecimalField("المبلغ الشهري", max_digits=12, decimal_places=2)
    token_encrypted = models.TextField(blank=True, editable=False)
    card_label = models.CharField("البطاقة", max_length=60, blank=True, editable=False)
    next_charge_on = models.DateField("موعد الخصم القادم")
    active = models.BooleanField("نشط", default=True)
    failures = models.PositiveSmallIntegerField("محاولات فاشلة", default=0, editable=False)
    last_order = models.ForeignKey(DonationOrder, models.SET_NULL, null=True, blank=True, related_name="+",
                                   editable=False, verbose_name="آخر طلب")
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)

    class Meta:
        verbose_name = "تبرع شهري"
        verbose_name_plural = "التبرعات الشهرية"
        ordering = ["-id"]

    def __str__(self):
        return f"{self.donor_name} — {self.opportunity} ({self.amount})"

    @property
    def token(self):
        from core.mail import decrypt_password

        return decrypt_password(self.token_encrypted)

    @token.setter
    def token(self, value):
        from core.mail import encrypt_password

        self.token_encrypted = encrypt_password(value)

    @property
    def available_actions(self):
        return [("deactivate", "إيقاف التبرع الشهري")] if self.active else [("reactivate", "إعادة التفعيل")]

    def deactivate(self, user=None):
        self.active = False
        self.save(update_fields=["active"])

    def reactivate(self, user=None):
        if not self.token_encrypted:
            raise ValidationError("لا توجد بطاقة محفوظة لهذا التبرع.")
        self.active, self.failures = True, 0
        if self.next_charge_on < timezone.localdate():
            self.next_charge_on = timezone.localdate()
        self.save(update_fields=["active", "failures", "next_charge_on"])


class DonorLogin(TenantModel):
    """رمز دخول المتبرع لحسابه برقم الجوال."""

    phone = models.CharField("الجوال", max_length=20)
    code_hash = models.CharField(max_length=64)
    sent_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "رمز دخول متبرع"
        verbose_name_plural = "رموز دخول المتبرعين"
