from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from core.tenancy import TenantManager, get_current_company, tenant_unique


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
    require_two_factor = models.BooleanField(
        "إلزام كل المستخدمين بالتحقق الثنائي", default=False,
        help_text="يطلب من كل مستخدم تفعيل رمز تطبيق المصادقة (Google/Microsoft Authenticator) قبل استخدام النظام")
    email = models.EmailField("البريد الإلكتروني", blank=True)
    address = models.TextField("العنوان", blank=True)
    default_tax_rate = models.DecimalField("نسبة ضريبة القيمة المضافة %", max_digits=5, decimal_places=2, default=15)
    update_buying_rate_on_purchase = models.BooleanField(
        "تحديث سعر الشراء في بطاقة الصنف من آخر شراء", default=True,
        help_text="عند اعتماد فاتورة مشتريات أو إذن استلام يصبح سعر الشراء = آخر سعر شراء، مع تنبيه إن تجاوز سعر البيع")

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
    default_exchange_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب فروق أسعار الصرف"
    )
    default_cost_center = models.ForeignKey(
        "accounts.CostCenter", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="مركز التكلفة الافتراضي"
    )
    default_retained_earnings_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب الأرباح المبقاة"
    )
    default_opening_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="حساب الأرصدة الافتتاحية"
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
    def current(cls):
        """الشركة الحالية للطلب، أو الشركة الوحيدة في النظام (لأوامر الإدارة والاختبارات)."""
        company = get_current_company()
        if company is not None:
            return company
        companies = list(cls.objects.all()[:2])
        return companies[0] if len(companies) == 1 else None

    def require_account(self, field):
        account = getattr(self, field)
        if account is None:
            label = self._meta.get_field(field).verbose_name
            raise ValidationError(f"يرجى تحديد «{label}» في إعدادات الشركة {self.name}.")
        return account


class TenantModel(models.Model):
    """نموذج مملوك لشركة؛ يُرشَّح تلقائياً بالشركة الحالية."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+", editable=False, verbose_name="الشركة")

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.company_id:
            company = Company.current()
            if company is None:
                raise ValidationError("لم يتم تحديد الشركة الحالية.")
            self.company = company
        super().save(*args, **kwargs)

    def validate_constraints(self, exclude=None):
        # حقل الشركة لا يظهر في النماذج، لكن قيود التفرد داخل الشركة يجب أن تُفحص
        if exclude and "company" in exclude:
            if not self.company_id and Company.current() is not None:
                self.company = Company.current()
            if self.company_id:
                exclude = set(exclude) - {"company"}
        super().validate_constraints(exclude=exclude)


class FiscalYear(TenantModel):
    name = models.CharField("السنة المالية", max_length=20)
    start_date = models.DateField("تاريخ البداية")
    end_date = models.DateField("تاريخ النهاية")
    is_closed = models.BooleanField("مقفلة", default=False)

    class Meta:
        verbose_name = "سنة مالية"
        verbose_name_plural = "السنوات المالية"
        ordering = ["-start_date"]
        constraints = [tenant_unique("name", model_label="fiscalyear")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValidationError("تاريخ النهاية يجب أن يكون بعد تاريخ البداية.")


class NamingSeries(TenantModel):
    prefix = models.CharField("البادئة", max_length=30)
    current = models.PositiveIntegerField("الرقم الحالي", default=0)

    class Meta:
        verbose_name = "سلسلة ترقيم"
        verbose_name_plural = "سلاسل الترقيم"
        constraints = [tenant_unique("prefix", model_label="namingseries")]

    def __str__(self):
        return f"{self.prefix} ({self.current})"

    @classmethod
    def next_name(cls, company, prefix):
        with transaction.atomic():
            cls.all_objects.get_or_create(company=company, prefix=prefix)
            series = cls.all_objects.select_for_update().get(company=company, prefix=prefix)
            series.current += 1
            series.save(update_fields=["current"])
            return f"{prefix}-{series.current:05d}"


class DocStatus(models.IntegerChoices):
    DRAFT = 0, "مسودة"
    SUBMITTED = 1, "معتمد"
    CANCELLED = 2, "ملغى"


class SubmittableDocument(TenantModel):
    """مستند قابل للاعتماد والإلغاء على غرار ERPNext (docstatus)."""

    naming_prefix = "DOC"

    name = models.CharField("الرقم", max_length=40, editable=False)
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
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="%(app_label)s_%(class)s_name_per_company"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.company_id:
            company = Company.current()
            if company is None:
                raise ValidationError("لم يتم تحديد الشركة الحالية.")
            self.company = company
        if not self.name:
            self.name = NamingSeries.next_name(self.company, f"{self.get_naming_prefix()}-{self.posting_date.year}")
        super().save(*args, **kwargs)

    def get_naming_prefix(self):
        return self.naming_prefix

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

    # المستندات التي لا تُرحّل قيوداً (عروض الأسعار والأوامر) لا تتقيد بالسنة المالية
    posts_to_ledger = True

    def validate_posting_period(self):
        """الترحيل مسموح فقط داخل سنة مالية مفتوحة للشركة."""
        if not self.posts_to_ledger:
            return
        year = FiscalYear.all_objects.filter(
            company=self.company, start_date__lte=self.posting_date, end_date__gte=self.posting_date,
        ).first()
        if year is None:
            raise ValidationError(
                f"لا توجد سنة مالية تشمل تاريخ {self.posting_date:%Y-%m-%d}. أضفها من «الإعدادات ← السنوات المالية»."
            )
        if year.is_closed:
            raise ValidationError(f"السنة المالية {year} مقفلة، ولا يمكن الترحيل أو الإلغاء فيها.")

    def submit(self):
        if not self.is_draft:
            raise ValidationError("يمكن اعتماد المسودات فقط.")
        with transaction.atomic():
            self.validate_posting_period()
            self.calculate_totals()
            self.validate_for_submit()
            self.docstatus = DocStatus.SUBMITTED
            self.save()
            self.on_submit()

    def cancel(self):
        if not self.is_submitted:
            raise ValidationError("يمكن إلغاء المستندات المعتمدة فقط.")
        with transaction.atomic():
            self.validate_posting_period()
            self.before_cancel()
            self.docstatus = DocStatus.CANCELLED
            self.save()
            self.on_cancel()


class Role(models.TextChoices):
    OWNER = "owner", "مالك الحساب"
    ADMIN = "admin", "مدير"
    USER = "user", "مستخدم"
    CASHIER = "cashier", "كاشير"
    EMPLOYEE = "employee", "موظف (الخدمة الذاتية)"


class Membership(models.Model):
    """عضوية مستخدم في شركة مع دوره فيها. المستخدم الواحد قد ينتمي لعدة شركات."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships", verbose_name="المستخدم")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="memberships", verbose_name="الشركة")
    role = models.CharField("الدور", max_length=10, choices=Role.choices, default=Role.USER)
    is_active = models.BooleanField("نشط", default=True)
    created_at = models.DateTimeField("تاريخ الإضافة", auto_now_add=True)
    # صلاحيات أدق للمستخدم العادي (المالك والمدير لهما كل الصلاحيات)
    modules = models.JSONField("الوحدات المسموحة", default=list, blank=True,
                               help_text="فارغة = كل الوحدات")
    can_submit = models.BooleanField("يعتمد ويلغي المستندات", default=True,
                                     help_text="إن أُلغي: يُنشئ المسودات فقط ويعتمدها غيره")
    cost_centers = models.ManyToManyField("accounts.CostCenter", blank=True, related_name="+",
                                          verbose_name="الفروع / مراكز التكلفة المسموحة",
                                          help_text="فارغة = كل الفروع")

    class Meta:
        verbose_name = "عضوية"
        verbose_name_plural = "أعضاء الشركات"
        constraints = [models.UniqueConstraint(fields=["user", "company"], name="membership_user_company")]

    def __str__(self):
        return f"{self.user} @ {self.company} ({self.get_role_display()})"

    @property
    def can_manage(self):
        return self.role in (Role.OWNER, Role.ADMIN)

    def allows_module(self, key):
        return self.can_manage or not self.modules or key in self.modules

    @property
    def may_submit(self):
        return self.can_manage or self.can_submit

    def allowed_cost_center_ids(self):
        """None = كل الفروع؛ وإلا مجموعة مراكز التكلفة المسموحة."""
        if self.can_manage or not self.pk:
            return None
        ids = set(self.cost_centers.values_list("id", flat=True))
        return ids or None


class ImportStatus(models.TextChoices):
    PREVIEWED = "previewed", "تمت المعاينة"
    DONE = "done", "تم الاستيراد"
    FAILED = "failed", "فشل"


class DataImport(TenantModel):
    """سجل عمليات الاستيراد مع الملف ونتيجة المعاينة والتنفيذ."""

    doctype_slug = models.CharField("نوع البيانات", max_length=50)
    file_name = models.CharField("اسم الملف", max_length=255)
    content = models.BinaryField("محتوى الملف")
    mode = models.CharField("طريقة الاستيراد", max_length=10, default="insert",
                            choices=[("insert", "إضافة سجلات جديدة"), ("update", "إضافة وتحديث الموجود")])
    submit_after = models.BooleanField("اعتماد المستندات بعد الاستيراد", default=False)
    valid_only = models.BooleanField("استيراد الصفوف الصحيحة فقط", default=False)
    status = models.CharField("الحالة", max_length=10, choices=ImportStatus.choices, default=ImportStatus.PREVIEWED)
    result = models.JSONField("النتيجة", default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField("التاريخ", auto_now_add=True)

    class Meta:
        verbose_name = "عملية استيراد"
        verbose_name_plural = "سجل الاستيراد"
        ordering = ["-created_at"]


class ExchangeRate(TenantModel):
    """سعر صرف عملة أجنبية مقابل عملة الشركة في تاريخ معين."""

    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, verbose_name="العملة")
    date = models.DateField("التاريخ", default=timezone.localdate)
    rate = models.DecimalField("سعر الصرف", max_digits=18, decimal_places=6,
                               help_text="قيمة وحدة واحدة من العملة الأجنبية بعملة الشركة")

    class Meta:
        verbose_name = "سعر صرف"
        verbose_name_plural = "أسعار الصرف"
        ordering = ["-date"]
        constraints = [tenant_unique("currency", "date", model_label="exchangerate")]

    def __str__(self):
        return f"{self.currency.code} {self.date:%Y-%m-%d} = {self.rate}"

    def clean(self):
        if self.rate is not None and self.rate <= 0:
            raise ValidationError({"rate": "سعر الصرف يجب أن يكون أكبر من صفر."})

    @classmethod
    def lookup(cls, company, currency, date):
        if currency.pk == company.currency_id:
            return 1
        found = cls.all_objects.filter(company=company, currency=currency, date__lte=date).order_by("-date").first()
        return found.rate if found else None


class Notification(TenantModel):
    """تنبيه داخل النظام لمستخدم في شركة (ويُرسل بالبريد أيضاً إن ضُبط خادم البريد)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, related_name="notifications",
                             verbose_name="المستخدم")
    title = models.CharField("التنبيه", max_length=255)
    url = models.CharField("الرابط", max_length=255, blank=True)
    is_read = models.BooleanField("مقروء", default=False)
    created_at = models.DateTimeField("الوقت", auto_now_add=True)

    class Meta:
        verbose_name = "تنبيه"
        verbose_name_plural = "التنبيهات"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class EmailSecurity(models.TextChoices):
    TLS = "tls", "STARTTLS (المنفذ 587 غالباً)"
    SSL = "ssl", "SSL/TLS (المنفذ 465 غالباً)"
    NONE = "none", "بدون تشفير (غير مستحسن)"


class EmailSettings(models.Model):
    """إعدادات خادم البريد (SMTP). سجل بلا شركة = إعدادات المنصة لكل الشركات،
    وسجل لشركة = خادم خاص بها يتقدم على إعدادات المنصة متى فُعّل."""

    company = models.OneToOneField(Company, models.CASCADE, null=True, blank=True, related_name="email_settings",
                                   verbose_name="الشركة")
    enabled = models.BooleanField("تفعيل إرسال البريد", default=False)
    host = models.CharField("خادم البريد (SMTP)", max_length=255, blank=True, help_text="مثل smtp.office365.com أو smtp.gmail.com")
    port = models.PositiveIntegerField("المنفذ", default=587)
    security = models.CharField("نوع التشفير", max_length=10, choices=EmailSecurity.choices, default=EmailSecurity.TLS)
    username = models.CharField("اسم المستخدم", max_length=255, blank=True, help_text="غالباً عنوان البريد نفسه")
    password_encrypted = models.TextField(blank=True, editable=False)
    from_email = models.EmailField("بريد المرسل", blank=True, help_text="العنوان الذي تظهر منه الرسائل")
    from_name = models.CharField("اسم المرسل", max_length=100, blank=True, help_text="مثل: الموارد البشرية - شركتي")
    site_url = models.URLField("رابط النظام", blank=True,
                               help_text="مثل https://erp.example.com — يُستخدم لإضافة روابط مباشرة داخل الرسائل")
    last_test_at = models.DateTimeField("آخر اختبار", null=True, blank=True, editable=False)
    last_test_ok = models.BooleanField(default=False, editable=False)
    last_test_message = models.CharField(max_length=500, blank=True, editable=False)

    class Meta:
        verbose_name = "إعدادات البريد"
        verbose_name_plural = "إعدادات البريد"
        constraints = [models.UniqueConstraint(fields=["company"], condition=models.Q(company__isnull=True),
                                               name="single_platform_email_settings")]

    def __str__(self):
        return f"بريد {self.company or 'المنصة'}"

    @classmethod
    def platform(cls):
        obj, _ = cls.objects.get_or_create(company=None)
        return obj

    @classmethod
    def for_company(cls, company):
        obj, _ = cls.objects.get_or_create(company=company)
        return obj

    @property
    def has_password(self):
        return bool(self.password_encrypted)

    @property
    def is_configured(self):
        return bool(self.host and self.from_email)


class UserTwoFactor(models.Model):
    """التحقق الثنائي برمز تطبيق المصادقة (TOTP) مع رموز استرداد لمرة واحدة."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, models.CASCADE, related_name="two_factor")
    secret_encrypted = models.TextField(editable=False)
    enabled = models.BooleanField(default=False)
    recovery_codes = models.JSONField(default=list, blank=True, editable=False)  # تجزئات SHA-256
    last_step = models.BigIntegerField(default=0, editable=False)  # يمنع إعادة استخدام نفس الرمز
    enabled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "تحقق ثنائي"
        verbose_name_plural = "التحقق الثنائي"


def has_two_factor(user):
    return UserTwoFactor.objects.filter(user=user, enabled=True).exists()


class ApiKey(models.Model):
    """مفتاح الواجهة البرمجية: مرتبط بمستخدم في شركة فتسري عليه صلاحياته. يُخزن مجزّأً ويظهر مرة واحدة."""

    company = models.ForeignKey(Company, models.CASCADE, related_name="api_keys", verbose_name="الشركة")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, related_name="api_keys",
                             verbose_name="يعمل بصلاحيات المستخدم")
    name = models.CharField("اسم المفتاح", max_length=100, help_text="مثل: ربط المتجر الإلكتروني")
    prefix = models.CharField(max_length=12, unique=True, editable=False)
    key_hash = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField("أنشئ في", auto_now_add=True)
    last_used_at = models.DateTimeField("آخر استخدام", null=True, blank=True)
    revoked = models.BooleanField("ملغى", default=False)

    class Meta:
        verbose_name = "مفتاح API"
        verbose_name_plural = "مفاتيح API"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def issue(cls, company, user, name):
        """ينشئ مفتاحاً ويعيد (السجل، المفتاح الكامل) — المفتاح لا يُخزن ولا يُعرض مرة أخرى."""
        import hashlib
        import secrets

        prefix = secrets.token_hex(4)
        raw = f"snr_{prefix}_{secrets.token_urlsafe(32)}"
        key = cls.objects.create(company=company, user=user, name=name, prefix=prefix,
                                 key_hash=hashlib.sha256(raw.encode()).hexdigest())
        return key, raw

    @classmethod
    def authenticate(cls, raw):
        import hashlib
        import hmac

        parts = (raw or "").split("_")
        if len(parts) < 3 or parts[0] != "snr":
            return None
        key = cls.objects.filter(prefix=parts[1], revoked=False).select_related("company", "user").first()
        if key is None or not hmac.compare_digest(key.key_hash, hashlib.sha256(raw.encode()).hexdigest()):
            return None
        return key


class PrintTemplate(models.TextChoices):
    CLASSIC = "classic", "كلاسيكي — جداول بإطارات واضحة"
    MODERN = "modern", "عصري — شريط علوي ملوّن"
    MINIMAL = "minimal", "بسيط — خطوط خفيفة ومساحات واسعة"


class PrintSettings(models.Model):
    """شكل المستندات المطبوعة للشركة: الشعار والقالب واللون والبيانات البنكية والتذييل."""

    company = models.OneToOneField(Company, models.CASCADE, related_name="print_settings")
    template = models.CharField("قالب الطباعة", max_length=10, choices=PrintTemplate.choices,
                                default=PrintTemplate.CLASSIC)
    accent_color = models.CharField("اللون الرئيسي", max_length=7, default="#1f4e79",
                                    help_text="لون العناوين ورأس الجداول، مثل #1f4e79")
    logo = models.TextField("الشعار", blank=True, editable=False)  # data URI (PNG/JPEG/WebP) لا يحتاج خادم ملفات
    name_en = models.CharField("اسم الشركة بالإنجليزية", max_length=150, blank=True)
    address_en = models.CharField("العنوان بالإنجليزية", max_length=250, blank=True)
    bilingual = models.BooleanField("عناوين عربية وإنجليزية", default=True,
                                    help_text="يضيف الترجمة الإنجليزية تحت عناوين المستند والجداول")
    show_amount_in_words = models.BooleanField("كتابة المبلغ بالحروف (تفقيط)", default=True)
    show_signatures = models.BooleanField("خانات التوقيع والختم", default=True)
    show_item_code = models.BooleanField("إظهار رمز الصنف", default=True)
    show_bank_details = models.BooleanField("إظهار البيانات البنكية في فواتير المبيعات", default=False)
    bank_name = models.CharField("اسم البنك", max_length=100, blank=True)
    bank_account_name = models.CharField("اسم صاحب الحساب", max_length=150, blank=True)
    iban = models.CharField("رقم الآيبان (IBAN)", max_length=34, blank=True)
    footer_text = models.CharField("نص التذييل", max_length=300, blank=True,
                                   help_text="يظهر أسفل كل صفحة، مثل: شكراً لتعاملكم معنا — الموقع الإلكتروني")
    sales_terms = models.TextField("الشروط الافتراضية لفواتير المبيعات", blank=True,
                                   help_text="تُطبع أسفل فواتير المبيعات التي لا تحمل شروطاً خاصة بها")

    class Meta:
        verbose_name = "إعدادات الطباعة"
        verbose_name_plural = "إعدادات الطباعة"

    def __str__(self):
        return f"إعدادات الطباعة — {self.company}"

    @classmethod
    def for_company(cls, company):
        obj = cls.objects.filter(company=company).first()
        return obj or cls(company=company)
