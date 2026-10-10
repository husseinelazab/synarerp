from django.db import models

from core.models import TenantModel

FIRST_INVOICE_PIH = "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="


class Environment(models.TextChoices):
    SANDBOX = "sandbox", "بيئة المطورين (تجريبية)"
    SIMULATION = "simulation", "بيئة المحاكاة"
    PRODUCTION = "production", "بيئة الإنتاج (فعلية)"


class OnboardingStatus(models.TextChoices):
    NOT_STARTED = "not_started", "لم يبدأ"
    COMPLIANCE = "compliance", "تم الحصول على شهادة الامتثال"
    CHECKED = "checked", "اجتاز فحوص الامتثال"
    ACTIVE = "active", "مفعّل (شهادة الإنتاج)"


class EInvoiceSettings(TenantModel):
    """إعدادات الربط مع منصة فاتورة لوحدة إصدار الفواتير (EGS) الخاصة بالشركة."""

    enabled = models.BooleanField("تفعيل الفوترة الإلكترونية", default=False)
    environment = models.CharField("البيئة", max_length=12, choices=Environment.choices, default=Environment.SANDBOX)
    auto_submit = models.BooleanField("الإرسال للهيئة فور اعتماد الفاتورة", default=True)
    # بيانات البائع كما تظهر في الفاتورة الإلكترونية
    seller_name = models.CharField("الاسم القانوني للبائع", max_length=200)
    vat_number = models.CharField("الرقم الضريبي", max_length=15)
    crn = models.CharField("السجل التجاري (CRN)", max_length=20)
    street = models.CharField("الشارع", max_length=100)
    building_number = models.CharField("رقم المبنى (4 أرقام)", max_length=4)
    district = models.CharField("الحي", max_length=100)
    city = models.CharField("المدينة", max_length=100)
    postal_code = models.CharField("الرمز البريدي (5 أرقام)", max_length=5)
    branch_name = models.CharField("اسم الفرع (الوحدة التنظيمية)", max_length=100, default="الفرع الرئيسي")
    industry = models.CharField("النشاط", max_length=100, default="تجارة")
    invoice_types = models.CharField("أنواع الفواتير", max_length=4, default="1100",
                                     choices=[("1100", "ضريبية (B2B) ومبسطة (B2C)"), ("1000", "ضريبية فقط"),
                                              ("0100", "مبسطة فقط")])
    egs_serial = models.CharField("الرقم التسلسلي للوحدة", max_length=100, editable=False)
    # بيانات التشفير والشهادات (مشفّرة)
    private_key = models.TextField(editable=False, blank=True)
    csr = models.TextField(editable=False, blank=True)
    compliance_token = models.TextField(editable=False, blank=True)
    compliance_secret = models.TextField(editable=False, blank=True)
    compliance_request_id = models.CharField(max_length=50, editable=False, blank=True)
    production_token = models.TextField(editable=False, blank=True)
    production_secret = models.TextField(editable=False, blank=True)
    status = models.CharField("حالة الربط", max_length=12, choices=OnboardingStatus.choices,
                              default=OnboardingStatus.NOT_STARTED, editable=False)
    compliance_results = models.JSONField(default=dict, blank=True, editable=False)
    # سلسلة الفواتير
    last_icv = models.PositiveIntegerField("عداد الفواتير", default=0, editable=False)
    last_hash = models.CharField("هاش آخر فاتورة", max_length=100, default=FIRST_INVOICE_PIH, editable=False)

    class Meta:
        verbose_name = "إعدادات الفوترة الإلكترونية"
        verbose_name_plural = "إعدادات الفوترة الإلكترونية"
        constraints = [models.UniqueConstraint(fields=["company"], name="einvoice_settings_per_company")]

    def __str__(self):
        return f"{self.seller_name} - {self.get_environment_display()}"

    @property
    def is_active(self):
        return self.enabled and self.status == OnboardingStatus.ACTIVE


class EInvoiceType(models.TextChoices):
    STANDARD = "standard", "فاتورة ضريبية (B2B)"
    SIMPLIFIED = "simplified", "فاتورة ضريبية مبسطة (B2C)"


class EInvoiceStatus(models.TextChoices):
    PENDING = "pending", "بانتظار الإرسال"
    REPORTED = "reported", "تم الإبلاغ"
    CLEARED = "cleared", "تم الاعتماد (Clearance)"
    WARNING = "warning", "مقبولة مع تحذيرات"
    REJECTED = "rejected", "مرفوضة"
    FAILED = "failed", "تعذر الإرسال"


class EInvoice(TenantModel):
    """الفاتورة الإلكترونية الصادرة لفاتورة مبيعات: XML الموقّع، الهاش، والسلسلة، وحالة الإرسال."""

    sales_invoice = models.OneToOneField("selling.SalesInvoice", on_delete=models.PROTECT, related_name="einvoice",
                                         verbose_name="فاتورة المبيعات")
    invoice_type = models.CharField("النوع", max_length=12, choices=EInvoiceType.choices)
    uuid = models.UUIDField("المعرف الفريد (UUID)")
    icv = models.PositiveIntegerField("عداد الفاتورة (ICV)")
    previous_hash = models.CharField("هاش الفاتورة السابقة (PIH)", max_length=100)
    invoice_hash = models.CharField("هاش الفاتورة", max_length=100)
    issue_datetime = models.DateTimeField("وقت الإصدار")
    xml = models.TextField("XML الموقّع")
    qr = models.TextField("رمز QR")
    cleared_xml = models.TextField("XML المعتمد من الهيئة", blank=True)
    status = models.CharField("الحالة", max_length=10, choices=EInvoiceStatus.choices, default=EInvoiceStatus.PENDING)
    response = models.JSONField("رد الهيئة", default=dict, blank=True)
    attempts = models.PositiveIntegerField("عدد محاولات الإرسال", default=0)
    submitted_at = models.DateTimeField("آخر إرسال", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "فاتورة إلكترونية"
        verbose_name_plural = "الفواتير الإلكترونية"
        ordering = ["-icv"]
        constraints = [models.UniqueConstraint(fields=["company", "icv"], name="einvoice_icv_per_company")]

    def __str__(self):
        return f"{self.sales_invoice} (ICV {self.icv})"

    @property
    def is_final(self):
        return self.status in (EInvoiceStatus.REPORTED, EInvoiceStatus.CLEARED, EInvoiceStatus.WARNING)

    @property
    def qr_for_print(self):
        """الفاتورة الضريبية المعتمدة تحمل رمز QR الذي تضيفه الهيئة؛ المبسطة تحمل رمزنا."""
        if self.cleared_xml:
            from einvoice.xml import extract_qr
            return extract_qr(self.cleared_xml) or self.qr
        return self.qr
