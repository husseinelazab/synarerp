from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone


class Currency(models.Model):
    code = models.CharField("رمز العملة", max_length=3, unique=True)
    name = models.CharField("اسم العملة", max_length=50)
    symbol = models.CharField("الرمز", max_length=10, blank=True)

    class Meta:
        verbose_name = "عملة"
        verbose_name_plural = "العملات"
        ordering = ["code"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Company(models.Model):
    name = models.CharField("اسم الشركة", max_length=150, unique=True)
    abbr = models.CharField("الاختصار", max_length=10)
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, verbose_name="العملة الافتراضية")
    tax_id = models.CharField("الرقم الضريبي", max_length=20, blank=True)
    commercial_register = models.CharField("السجل التجاري", max_length=30, blank=True)
    phone = models.CharField("الهاتف", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    address = models.TextField("العنوان", blank=True)
    default_tax_rate = models.DecimalField("نسبة ضريبة القيمة المضافة %", max_digits=5, decimal_places=2, default=15)

    default_cash_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب النقدية الافتراضي"
    )
    default_receivable_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب العملاء (المدينون)"
    )
    default_payable_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب الموردين (الدائنون)"
    )
    default_income_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب الإيرادات الافتراضي"
    )
    default_expense_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب المصروفات الافتراضي"
    )
    default_stock_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب المخزون"
    )
    default_cogs_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب تكلفة البضاعة المباعة"
    )
    default_stock_adjustment_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب تسويات المخزون"
    )
    default_output_tax_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب ضريبة المخرجات (المبيعات)"
    )
    default_input_tax_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب ضريبة المدخلات (المشتريات)"
    )
    default_discount_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب الخصم المسموح به"
    )

    class Meta:
        verbose_name = "شركة"
        verbose_name_plural = "الشركات"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def get_default(cls):
        return cls.objects.order_by("id").first()

    def require_account(self, field):
        account = getattr(self, field)
        if account is None:
            label = self._meta.get_field(field).verbose_name
            raise ValidationError(f"يرجى تحديد «{label}» في إعدادات الشركة {self.name}.")
        return account


class FiscalYear(models.Model):
    name = models.CharField("السنة المالية", max_length=20, unique=True)
    start_date = models.DateField("تاريخ البداية")
    end_date = models.DateField("تاريخ النهاية")
    is_closed = models.BooleanField("مقفلة", default=False)

    class Meta:
        verbose_name = "سنة مالية"
        verbose_name_plural = "السنوات المالية"
        ordering = ["-start_date"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValidationError("تاريخ النهاية يجب أن يكون بعد تاريخ البداية.")


class NamingSeries(models.Model):
    prefix = models.CharField("البادئة", max_length=30, unique=True)
    current = models.PositiveIntegerField("الرقم الحالي", default=0)

    class Meta:
        verbose_name = "سلسلة ترقيم"
        verbose_name_plural = "سلاسل الترقيم"

    def __str__(self):
        return f"{self.prefix} ({self.current})"

    @classmethod
    def next_name(cls, prefix):
        with transaction.atomic():
            cls.objects.get_or_create(prefix=prefix)
            series = cls.objects.select_for_update().get(prefix=prefix)
            series.current += 1
            series.save(update_fields=["current"])
            return f"{prefix}-{series.current:05d}"


class DocStatus(models.IntegerChoices):
    DRAFT = 0, "مسودة"
    SUBMITTED = 1, "معتمد"
    CANCELLED = 2, "ملغى"


class SubmittableDocument(models.Model):
    """مستند قابل للاعتماد والإلغاء على غرار ERPNext (docstatus)."""

    naming_prefix = "DOC"

    name = models.CharField("الرقم", max_length=40, unique=True, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="+", editable=False, verbose_name="الشركة")
    posting_date = models.DateField("التاريخ", default=timezone.localdate)
    docstatus = models.PositiveSmallIntegerField("الحالة", choices=DocStatus.choices, default=DocStatus.DRAFT, editable=False)
    remarks = models.TextField("ملاحظات", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+", editable=False, verbose_name="أنشئ بواسطة"
    )
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    modified_at = models.DateTimeField("آخر تعديل", auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-posting_date", "-id"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.company_id:
            company = Company.get_default()
            if company is None:
                raise ValidationError("لا توجد شركة معرّفة. يرجى تشغيل أمر الإعداد setup_erp أولاً.")
            self.company = company
        if not self.name:
            self.name = NamingSeries.next_name(f"{self.naming_prefix}-{self.posting_date.year}")
        super().save(*args, **kwargs)

    @property
    def is_draft(self):
        return self.docstatus == DocStatus.DRAFT

    @property
    def is_submitted(self):
        return self.docstatus == DocStatus.SUBMITTED

    @property
    def is_cancelled(self):
        return self.docstatus == DocStatus.CANCELLED

    def get_absolute_url(self):
        return reverse("doc_detail", args=[self.doctype_slug, self.pk])

    # خطافات تعيد تعريفها المستندات
    def calculate_totals(self):
        pass

    def validate_for_submit(self):
        pass

    def on_submit(self):
        pass

    def before_cancel(self):
        pass

    def on_cancel(self):
        pass

    def submit(self):
        if not self.is_draft:
            raise ValidationError("يمكن اعتماد المسودات فقط.")
        with transaction.atomic():
            self.calculate_totals()
            self.validate_for_submit()
            self.docstatus = DocStatus.SUBMITTED
            self.save()
            self.on_submit()

    def cancel(self):
        if not self.is_submitted:
            raise ValidationError("يمكن إلغاء المستندات المعتمدة فقط.")
        with transaction.atomic():
            self.before_cancel()
            self.docstatus = DocStatus.CANCELLED
            self.save()
            self.on_cancel()
