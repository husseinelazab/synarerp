"""ربط المتاجر الإلكترونية (سلة وزد): كل طلب يصل من المتجر يصبح فاتورة مبيعات تُخصم من المخزون."""
import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

from core.models import TenantModel
from core.tenancy import tenant_unique


class Platform(models.TextChoices):
    SALLA = "salla", "سلة (Salla)"
    ZID = "zid", "زد (Zid)"


class CustomerMode(models.TextChoices):
    PER_CUSTOMER = "per_customer", "عميل لكل مشترٍ (بالجوال)"
    SINGLE = "single", "عميل واحد لكل الطلبات"


def new_key():
    return secrets.token_urlsafe(24)


class StoreConnection(TenantModel):
    platform = models.CharField("المنصة", max_length=10, choices=Platform.choices)
    name = models.CharField("اسم المتجر", max_length=100)
    store_id = models.CharField("رقم المتجر في المنصة", max_length=50, blank=True,
                                help_text="اختياري: رقم التاجر (Merchant / Store ID) للتحقق من أن الطلبات من متجرك")
    webhook_key = models.CharField("مفتاح الرابط", max_length=64, unique=True, default=new_key, editable=False)
    webhook_secret = models.CharField(
        "سر الـ Webhook", max_length=200, blank=True,
        help_text="سلة: «Webhook Secret» من لوحة الشركاء (يُتحقق به من التوقيع X-Salla-Signature أو ترويسة Authorization). "
                  "زد: اختياري")
    enabled = models.BooleanField("مفعّل", default=True)
    customer_mode = models.CharField("العملاء", max_length=15, choices=CustomerMode.choices,
                                     default=CustomerMode.PER_CUSTOMER)
    default_customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, related_name="+",
                                         verbose_name="العميل الافتراضي",
                                         help_text="لطلبات بلا بيانات مشترٍ، أو لكل الطلبات في وضع «عميل واحد»")
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+", verbose_name="مستودع المتجر",
                                  limit_choices_to={"disabled": False})
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False}, verbose_name="مركز التكلفة / الفرع")
    prices_include_tax = models.BooleanField("أسعار المتجر شاملة الضريبة", default=True)
    tax_rate = models.DecimalField("نسبة الضريبة %", max_digits=5, decimal_places=2, default=15)
    shipping_item = models.ForeignKey("stock.Item", models.PROTECT, null=True, blank=True, related_name="+",
                                      limit_choices_to={"is_stock_item": False, "disabled": False},
                                      verbose_name="صنف رسوم الشحن", help_text="خدمة غير مخزنية؛ تُنشأ تلقائياً إن تُركت فارغة")
    payment_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+",
        limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]},
        verbose_name="حساب تحصيلات المتجر",
        help_text="الطلبات المدفوعة إلكترونياً تُسجل لها سندات قبض في هذا الحساب (مستحقات بوابة الدفع). "
                  "الدفع عند الاستلام يبقى ذمة على العميل")
    auto_submit = models.BooleanField("اعتماد الفواتير تلقائياً", default=True,
                                      help_text="وإلا تُنشأ مسودات للمراجعة")
    last_event_at = models.DateTimeField("آخر طلب وارد", null=True, blank=True, editable=False)

    # الوصول لواجهة المنصة (المرحلة الثانية: مزامنة المخزون واستيراد المنتجات) — الرموز تُحفظ مشفرة
    access_token_enc = models.TextField(blank=True, editable=False)
    refresh_token_enc = models.TextField(blank=True, editable=False)
    partner_token_enc = models.TextField(blank=True, editable=False)
    client_secret_enc = models.TextField(blank=True, editable=False)
    token_expires_at = models.DateTimeField("انتهاء رمز الوصول", null=True, blank=True, editable=False)
    client_id = models.CharField("Client ID للتطبيق", max_length=100, blank=True,
                                 help_text="سلة: لتجديد رمز الوصول تلقائياً قبل انتهائه")
    sync_stock = models.BooleanField("مزامنة كميات المخزون مع المتجر", default=False,
                                     help_text="كل ساعة تُرسل كميات الأصناف المربوطة من مستودع المتجر")
    stock_buffer = models.DecimalField("كمية احتياطية لا تُعرض في المتجر", max_digits=12, decimal_places=3, default=0,
                                       help_text="تُطرح من الرصيد قبل إرساله، لتفادي بيع ما بيع للتو في الفرع")
    last_stock_sync_at = models.DateTimeField("آخر مزامنة للمخزون", null=True, blank=True, editable=False)
    last_sync_message = models.CharField("نتيجة آخر مزامنة", max_length=255, blank=True, editable=False)

    class Meta:
        verbose_name = "متجر إلكتروني"
        verbose_name_plural = "المتاجر الإلكترونية (سلة وزد)"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="storeconnection")]

    def __str__(self):
        return f"{self.name} ({self.get_platform_display()})"

    def get_absolute_url(self):
        return reverse("doc_detail", args=["store-connection", self.pk])

    def webhook_path(self):
        return reverse("ecommerce_webhook", args=[self.platform, self.webhook_key])

    def _secret(self, field, value=None):
        from core.mail import decrypt_password, encrypt_password

        if value is None:
            return decrypt_password(getattr(self, field))
        setattr(self, field, encrypt_password(value))

    @property
    def access_token(self):
        return self._secret("access_token_enc")

    @property
    def has_api_access(self):
        return bool(self.access_token_enc)

    def set_tokens(self, access="", refresh=None, expires_in=None, partner=None, client_secret=None):
        import datetime

        from django.utils import timezone

        if access is not None:
            self._secret("access_token_enc", access)
        if refresh is not None:
            self._secret("refresh_token_enc", refresh)
        if partner is not None:
            self._secret("partner_token_enc", partner)
        if client_secret is not None:
            self._secret("client_secret_enc", client_secret)
        if expires_in:
            expires_in = int(expires_in)
            # سلة ترسل وقت الانتهاء (طابع زمني) في حدث التفويض، و«المدة بالثواني» عند التجديد
            self.token_expires_at = (datetime.datetime.fromtimestamp(expires_in, tz=datetime.timezone.utc)
                                     if expires_in > 10 ** 9 else timezone.now() + datetime.timedelta(seconds=expires_in))

    def clean(self):
        if self.customer_mode == CustomerMode.SINGLE and not self.default_customer_id:
            raise ValidationError({"default_customer": "حدد العميل الذي تُسجل عليه كل الطلبات."})


class SkuMapping(TenantModel):
    """ربط رمز SKU في المتجر بصنف في النظام عندما لا يطابق كود الصنف أو باركوده."""

    connection = models.ForeignKey(StoreConnection, models.CASCADE, related_name="sku_mappings", verbose_name="المتجر")
    sku = models.CharField("SKU في المتجر", max_length=100)
    item = models.ForeignKey("stock.Item", models.PROTECT, related_name="+", verbose_name="الصنف",
                             limit_choices_to={"disabled": False})
    uom = models.ForeignKey("stock.UOM", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="الوحدة",
                            help_text="فارغة = الأساسية (مثلاً: SKU الكرتون ← وحدة كرتون)")

    class Meta:
        verbose_name = "ربط SKU"
        verbose_name_plural = "ربط أصناف المتجر"
        ordering = ["sku"]
        constraints = [models.UniqueConstraint(fields=["connection", "sku"], name="ecommerce_sku_per_connection")]

    def __str__(self):
        return f"{self.sku} ← {self.item}"


class OrderStatus(models.TextChoices):
    IMPORTED = "imported", "تم إنشاء الفاتورة"
    REVIEW = "review", "يحتاج مراجعة"
    CANCELLED = "cancelled", "ملغى في المتجر"
    IGNORED = "ignored", "متجاهل"


class ExternalOrder(TenantModel):
    connection = models.ForeignKey(StoreConnection, models.CASCADE, related_name="orders", verbose_name="المتجر")
    external_id = models.CharField("معرّف الطلب في المنصة", max_length=64)
    number = models.CharField("رقم الطلب", max_length=64, blank=True)
    customer_name = models.CharField("المشتري", max_length=200, blank=True)
    total = models.DecimalField("إجمالي الطلب", max_digits=18, decimal_places=2, default=0)
    status = models.CharField("الحالة", max_length=10, choices=OrderStatus.choices, default=OrderStatus.REVIEW)
    error = models.TextField("سبب المراجعة", blank=True)
    payload = models.JSONField("بيانات الطلب كما وصلت", default=dict)
    invoice = models.ForeignKey("selling.SalesInvoice", models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="الفاتورة")
    payment = models.ForeignKey("accounts.PaymentEntry", models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="سند القبض")
    received_at = models.DateTimeField("وقت الوصول", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        verbose_name = "طلب متجر"
        verbose_name_plural = "طلبات المتاجر الإلكترونية"
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["connection", "external_id"], name="ecommerce_order_once")]

    def __str__(self):
        return f"{self.connection.name} #{self.number or self.external_id}"

    def get_absolute_url(self):
        return reverse("ecommerce_order", args=[self.pk])


class StoreProduct(TenantModel):
    """منتج في المتجر (من استيراد المنتجات) وربطه بصنف النظام؛ عليه تُبنى مزامنة الكميات."""

    connection = models.ForeignKey(StoreConnection, models.CASCADE, related_name="products", verbose_name="المتجر")
    external_id = models.CharField("معرّف المنتج في المنصة", max_length=64)
    sku = models.CharField("SKU", max_length=100, blank=True)
    name = models.CharField("الاسم في المتجر", max_length=255, blank=True)
    price = models.DecimalField("السعر في المتجر", max_digits=18, decimal_places=2, null=True, blank=True)
    store_quantity = models.DecimalField("الكمية في المتجر", max_digits=18, decimal_places=3, null=True, blank=True)
    item = models.ForeignKey("stock.Item", models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="الصنف")
    last_pushed_qty = models.DecimalField("آخر كمية أُرسلت", max_digits=18, decimal_places=3, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "منتج متجر"
        verbose_name_plural = "منتجات المتجر"
        ordering = ["sku", "name"]
        constraints = [models.UniqueConstraint(fields=["connection", "external_id"], name="ecommerce_product_once")]

    def __str__(self):
        return f"{self.sku or self.external_id} — {self.name}"
