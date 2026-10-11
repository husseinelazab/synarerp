from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.models import SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt
from core.vat import REASON_CHOICES, VATCategory
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry


class UOM(TenantModel):
    name = models.CharField("وحدة القياس", max_length=50)

    class Meta:
        verbose_name = "وحدة قياس"
        verbose_name_plural = "وحدات القياس"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="uom")]

    def __str__(self):
        return self.name


class ItemGroup(TenantModel):
    name = models.CharField("اسم المجموعة", max_length=100)
    parent = models.ForeignKey("self", models.PROTECT, null=True, blank=True, related_name="children", verbose_name="المجموعة الأم")

    class Meta:
        verbose_name = "مجموعة أصناف"
        verbose_name_plural = "مجموعات الأصناف"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="itemgroup")]

    def __str__(self):
        return self.name


class Item(TenantModel):
    code = models.CharField("كود الصنف", max_length=50)
    name = models.CharField("اسم الصنف", max_length=200)
    item_group = models.ForeignKey(ItemGroup, models.PROTECT, verbose_name="مجموعة الأصناف")
    uom = models.ForeignKey(UOM, models.PROTECT, verbose_name="وحدة القياس")
    is_stock_item = models.BooleanField("صنف مخزني", default=True, help_text="ألغِ التحديد للخدمات والأصناف غير المخزنية")
    selling_rate = models.DecimalField("سعر البيع", max_digits=18, decimal_places=2, default=0)
    buying_rate = models.DecimalField("سعر الشراء", max_digits=18, decimal_places=2, default=0)
    barcode = models.CharField("الباركود", max_length=50, blank=True)
    income_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+",
        limit_choices_to={"is_group": False, "root_type": "income"}, verbose_name="حساب الإيراد",
    )
    expense_account = models.ForeignKey(
        "accounts.Account", models.SET_NULL, null=True, blank=True, related_name="+",
        limit_choices_to={"is_group": False, "root_type": "expense"}, verbose_name="حساب المصروف",
    )
    description = models.TextField("الوصف", blank=True)
    vat_category = models.CharField("فئة ضريبة القيمة المضافة", max_length=1, choices=VATCategory.choices,
                                    default=VATCategory.STANDARD)
    vat_exemption_reason = models.CharField("سبب الإعفاء / النسبة الصفرية", max_length=20, choices=REASON_CHOICES,
                                            blank=True)
    shelf_location = models.CharField("موقع الرف", max_length=30, blank=True,
                                      help_text="مثل A-03-2 (الممر-الرف-المستوى)؛ يُطبع على ملصق الرف")
    is_weighed = models.BooleanField("يُباع بالوزن (ميزان)", default=False,
                                     help_text="يُرسل سعره للموازين، ويُقرأ باركود الميزان المطبوع عليه بالوزن")
    plu = models.PositiveIntegerField("رقم PLU في الميزان", null=True, blank=True,
                                      help_text="رقم الصنف داخل الميزان، ويظهر في باركود الميزان")
    scale_name = models.CharField("الاسم على الميزان", max_length=40, blank=True,
                                  help_text="اختياري: اسم مختصر يُطبع على ملصق الميزان (بعض الموازين لا تدعم العربية)")
    shelf_life_days = models.PositiveSmallIntegerField("مدة الصلاحية بالأيام", null=True, blank=True,
                                                       help_text="يُحسب منها تاريخ الانتهاء للتشغيلات الجديدة وملصق الميزان")
    has_batch_no = models.BooleanField("تتبع التشغيلات والصلاحية", default=False,
                                       help_text="يُتابَع رصيد كل تشغيلة بتاريخ انتهائها، ويُصرف الأقرب انتهاءً أولاً، ويُمنع بيع المنتهي")
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "صنف"
        verbose_name_plural = "الأصناف"
        ordering = ["code"]
        constraints = [tenant_unique("code", model_label="item"), tenant_unique("plu", model_label="item")]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs):
        # تسجيل تغيّر سعري البيع والشراء (من التعديل اليدوي أو الاستيراد أو آخر شراء)
        old = None
        if self.pk:
            old = Item.all_objects.filter(pk=self.pk).values("selling_rate", "buying_rate").first()
        super().save(*args, **kwargs)
        if self.has_batch_no:
            from stock.batches import sync_initial_batches

            sync_initial_batches(self)
        fields = kwargs.get("update_fields")
        if old and (fields is None or {"selling_rate", "buying_rate"} & set(fields)):
            from stock.pricing import record_price_changes

            record_price_changes(self, old)

    def clean(self):
        super().clean()
        if self.barcode and self.company_id and ItemUOM.all_objects.filter(
                company_id=self.company_id, barcode=self.barcode).exists():
            raise ValidationError({"barcode": "هذا الباركود مستخدم لوحدة تحويل صنف آخر."})
        if self.is_weighed:
            if not self.plu:
                raise ValidationError({"plu": "أدخل رقم PLU للصنف الذي يُباع بالوزن."})
            settings = ScaleSettings.objects.filter(company_id=self.company_id).first() if self.company_id else None
            length = settings.plu_length if settings else 5
            if len(str(self.plu)) > length:
                raise ValidationError({"plu": f"رقم PLU أطول من {length} أرقام المحددة في إعدادات باركود الموازين."})

    def uom_factor(self, uom_id):
        """معامل تحويل وحدة إلى الوحدة الأساسية (1 للأساسية)."""
        if not uom_id or uom_id == self.uom_id:
            return Decimal(1)
        row = ItemUOM.all_objects.filter(item_id=self.pk, uom_id=uom_id).first()
        if row is None:
            name = UOM.all_objects.filter(pk=uom_id).values_list("name", flat=True).first() or uom_id
            raise ValidationError(f"الوحدة «{name}» غير معرّفة للصنف «{self.name}». أضفها في وحدات التحويل.")
        return row.factor

    def price_for(self, uom_id=None, kind="selling"):
        """سعر البيع أو الشراء بوحدة معينة: سعر الوحدة المحدد، وإلا سعر الأساسية × المعامل."""
        factor = self.uom_factor(uom_id)
        if kind == "selling" and uom_id and uom_id != self.uom_id:
            row = ItemUOM.all_objects.filter(item_id=self.pk, uom_id=uom_id).first()
            if row and row.selling_rate is not None:
                return row.selling_rate
        base = self.selling_rate if kind == "selling" else self.buying_rate
        return flt(Decimal(base) * factor)

    def uom_options(self):
        """الوحدات المتاحة: [{uom, name, factor, barcode, selling_rate, buying_rate}] بدءاً بالأساسية."""
        rows = [{"uom": self.uom_id, "name": str(self.uom), "factor": Decimal(1), "barcode": self.barcode,
                 "selling_rate": self.selling_rate, "buying_rate": self.buying_rate, "base": True}]
        for u in self.uoms.select_related("uom").order_by("factor"):
            rows.append({"uom": u.uom_id, "name": str(u.uom), "factor": u.factor, "barcode": u.barcode,
                         "selling_rate": u.selling_rate if u.selling_rate is not None
                         else flt(self.selling_rate * u.factor),
                         "buying_rate": flt(self.buying_rate * u.factor), "base": False})
        return rows

    @classmethod
    def match_code(cls, code):
        """(الصنف، وحدة التحويل المطابقة أو None) من باركود أو كود."""
        item = cls.find_by_code(code)
        if item is not None:
            return item, None
        code = (code or "").strip()
        if not code:
            return None, None
        candidates = [code]
        if code.isdigit() and len(code) == 12:
            candidates.append("0" + code)
        elif code.isdigit() and len(code) == 13 and code.startswith("0"):
            candidates.append(code[1:])
        unit = (ItemUOM.objects.filter(barcode__in=candidates, item__disabled=False)
                .select_related("item", "item__uom", "uom").first())
        return (unit.item, unit) if unit else (None, None)

    @classmethod
    def resolve_code(cls, code):
        """قراءة الماسح: (الصنف، الكمية من باركود الميزان أو None) أو (None، None)."""
        item, unit = cls.match_code(code)
        if item is not None:
            return item, (unit.factor if unit else None)
        from stock.scale import read_scale_barcode

        return read_scale_barcode(code)

    @classmethod
    def find_by_code(cls, code, queryset=None):
        """البحث بالباركود أو كود الصنف كما يقرؤه الماسح (مع مطابقة UPC-A وEAN-13 بالصفر البادئ)."""
        code = (code or "").strip()
        if not code:
            return None
        qs = queryset if queryset is not None else cls.objects.filter(disabled=False)
        candidates = [code]
        if code.isdigit() and len(code) == 12:
            candidates.append("0" + code)
        elif code.isdigit() and len(code) == 13 and code.startswith("0"):
            candidates.append(code[1:])
        item = qs.filter(barcode__in=candidates).order_by("id").first()
        return item or qs.filter(code__iexact=code).first()


class ItemUOM(TenantModel):
    """وحدة تحويل للصنف: مثل كرتون = 24 حبة، بباركود وسعر بيع خاصين."""

    item = models.ForeignKey(Item, models.CASCADE, related_name="uoms", verbose_name="الصنف",
                             limit_choices_to={"disabled": False})
    uom = models.ForeignKey(UOM, models.PROTECT, related_name="+", verbose_name="الوحدة")
    factor = models.DecimalField("المعامل (كم وحدة أساسية)", max_digits=18, decimal_places=6,
                                 help_text="عدد الوحدات الأساسية في هذه الوحدة، مثل 24 للكرتون إذا كانت الأساسية «حبة»")
    barcode = models.CharField("الباركود", max_length=50, blank=True)
    selling_rate = models.DecimalField("سعر البيع", max_digits=18, decimal_places=2, null=True, blank=True,
                                       help_text="اتركه فارغاً ليُحسب: سعر الوحدة الأساسية × المعامل")

    class Meta:
        verbose_name = "وحدة تحويل"
        verbose_name_plural = "وحدات التحويل"
        ordering = ["item__code", "factor"]
        constraints = [tenant_unique("item", "uom", model_label="itemuom")]

    def __str__(self):
        return f"{self.item.name} — {self.uom} = {flt(self.factor, 6).normalize():f} {self.item.uom}"

    def clean(self):
        super().clean()
        if self.factor is not None and self.factor <= 0:
            raise ValidationError({"factor": "المعامل يجب أن يكون أكبر من صفر."})
        if self.item_id and self.uom_id and self.uom_id == self.item.uom_id:
            raise ValidationError({"uom": "هذه هي الوحدة الأساسية للصنف؛ اختر وحدة أخرى."})
        if self.barcode:
            company = self.company_id or (self.item.company_id if self.item_id else None)
            clash = Item.all_objects.filter(company_id=company, barcode=self.barcode).exists() or \
                ItemUOM.all_objects.filter(company_id=company, barcode=self.barcode).exclude(pk=self.pk).exists()
            if clash:
                raise ValidationError({"barcode": "هذا الباركود مستخدم لصنف أو وحدة أخرى."})

    @property
    def effective_selling_rate(self):
        return self.selling_rate if self.selling_rate is not None else flt(self.item.selling_rate * self.factor)


class ItemPriceHistory(TenantModel):
    """سجل تغيّر أسعار الأصناف: من غيّر، ومتى، ومن أي مصدر."""

    SOURCES = [("manual", "تعديل يدوي"), ("import", "استيراد ملف"), ("purchase", "آخر سعر شراء")]
    FIELDS = [("selling_rate", "سعر البيع"), ("buying_rate", "سعر الشراء")]

    item = models.ForeignKey(Item, models.CASCADE, related_name="price_history", verbose_name="الصنف")
    field = models.CharField("السعر", max_length=20, choices=FIELDS)
    old_rate = models.DecimalField("السعر السابق", max_digits=18, decimal_places=2)
    new_rate = models.DecimalField("السعر الجديد", max_digits=18, decimal_places=2)
    source = models.CharField("المصدر", max_length=10, choices=SOURCES, default="manual")
    reference = models.CharField("المرجع", max_length=60, blank=True)
    user = models.ForeignKey("auth.User", models.SET_NULL, null=True, blank=True, related_name="+",
                             verbose_name="المستخدم")
    created_at = models.DateTimeField("الوقت", auto_now_add=True)

    class Meta:
        verbose_name = "تغيير سعر"
        verbose_name_plural = "سجل تغيّر الأسعار"
        ordering = ["-id"]

    @property
    def change_percent(self):
        return flt((self.new_rate - self.old_rate) * 100 / self.old_rate) if self.old_rate else None


class ScaleValueType(models.TextChoices):
    WEIGHT = "weight", "الوزن بالجرام (الأكثر شيوعاً)"
    PRICE = "price", "السعر بالهللة"


class ScaleSettings(TenantModel):
    """صيغة باركود الموازين: بادئة + رقم PLU + الوزن أو السعر + رقم تحقق (EAN-13).

    مثال بالإعداد الافتراضي: 21 00042 01250 C ← الصنف PLU 42 بوزن 1.250 كجم.
    """

    enabled = models.BooleanField("تفعيل قراءة باركود الموازين", default=False)
    prefixes = models.CharField("بادئات باركود الميزان", max_length=60, default="21",
                                help_text="رقمان أو رقم واحد، ويمكن أكثر من بادئة مفصولة بفاصلة مثل: 21,22")
    plu_length = models.PositiveSmallIntegerField("عدد أرقام PLU في الباركود", default=5)
    value_type = models.CharField("ما يحمله الباركود", max_length=10, choices=ScaleValueType.choices,
                                  default=ScaleValueType.WEIGHT)

    class Meta:
        verbose_name = "إعدادات باركود الموازين"
        verbose_name_plural = "إعدادات باركود الموازين"
        constraints = [models.UniqueConstraint(fields=["company"], name="one_scale_settings_per_company")]

    @property
    def prefix_list(self):
        return [p.strip() for p in self.prefixes.split(",") if p.strip()]

    @property
    def prefix_length(self):
        return len(self.prefix_list[0]) if self.prefix_list else 2

    @property
    def value_length(self):
        return 12 - self.prefix_length - self.plu_length

    def clean(self):
        super().clean()
        prefixes = self.prefix_list
        if not prefixes or any(not p.isdigit() or len(p) not in (1, 2) for p in prefixes):
            raise ValidationError({"prefixes": "البادئة رقم أو رقمان، مثل 2 أو 21."})
        if len({len(p) for p in prefixes}) > 1:
            raise ValidationError({"prefixes": "كل البادئات يجب أن تكون بنفس الطول."})
        if not 3 <= self.plu_length <= 6:
            raise ValidationError({"plu_length": "عدد أرقام PLU بين 3 و6."})
        if self.value_length < 4:
            raise ValidationError({"plu_length": "لا يبقى مكان كافٍ للوزن/السعر في الباركود؛ قلّل أرقام PLU."})

    def example(self, plu=42, value=1250):
        from stock.barcode import ean_checksum

        body = self.prefix_list[0] + str(plu).zfill(self.plu_length) + str(value).zfill(self.value_length)
        return body + ean_checksum(body)

    def parse(self, code):
        """يعيد (رقم PLU، القيمة كعدد صحيح) أو None إن لم يكن باركود ميزان."""
        from stock.barcode import ean_checksum

        code = (code or "").strip()
        if not self.enabled or not code.isdigit() or len(code) != 13 or ean_checksum(code[:12]) != code[12]:
            return None
        if not any(code.startswith(p) for p in self.prefix_list):
            return None
        start = self.prefix_length
        plu = int(code[start:start + self.plu_length])
        value = int(code[start + self.plu_length:12])
        return plu, value

    def config(self):
        """إعدادات القراءة لشاشة نقطة البيع (تعمل دون اتصال)."""
        return {"enabled": self.enabled, "prefixes": self.prefix_list, "pluLength": self.plu_length,
                "valueType": self.value_type}

    @classmethod
    def for_company(cls, company):
        return cls.objects.filter(company=company).first() or cls(company=company)


class ScaleDriver(models.TextChoices):
    FILE = "file", "ملف استيراد لبرنامج الميزان (CSV)"
    CUSTOM = "custom", "مشغّل مباشر خاص بالشركة المصنعة"


class Scale(TenantModel):
    """ميزان طباعة ملصقات في الفرع. البرنامج الوسيط في الفرع يجلب أسعاره دورياً ويرسلها إليه."""

    name = models.CharField("اسم الميزان", max_length=80)
    brand = models.CharField("الشركة المصنعة / الطراز", max_length=80, blank=True, help_text="مثل CAS CL5200 أو DIGI SM-120")
    driver = models.CharField("طريقة الإرسال", max_length=10, choices=ScaleDriver.choices, default=ScaleDriver.FILE)
    ip_address = models.GenericIPAddressField("عنوان IP في الشبكة المحلية", null=True, blank=True)
    port = models.PositiveIntegerField("المنفذ", null=True, blank=True)
    item_groups = models.ManyToManyField(ItemGroup, blank=True, related_name="+", verbose_name="مجموعات الأصناف",
                                         help_text="اتركها فارغة لإرسال كل الأصناف الموزونة")
    enabled = models.BooleanField("مفعّل", default=True)
    last_sync_at = models.DateTimeField("آخر مزامنة", null=True, blank=True, editable=False)
    last_sync_ok = models.BooleanField("نجحت آخر مزامنة", null=True, editable=False)
    last_sync_message = models.CharField("نتيجة آخر مزامنة", max_length=300, blank=True, editable=False)
    last_plu_count = models.PositiveIntegerField("عدد الأصناف المرسلة", default=0, editable=False)
    last_version = models.CharField("نسخة الأسعار المرسلة", max_length=64, blank=True, editable=False)

    class Meta:
        verbose_name = "ميزان"
        verbose_name_plural = "الموازين"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="scale")]

    def __str__(self):
        return self.name

    def items(self):
        qs = Item.objects.filter(is_weighed=True, disabled=False, plu__isnull=False).select_related("uom", "item_group")
        groups = list(self.item_groups.values_list("id", flat=True)) if self.pk else []
        if groups:
            qs = qs.filter(item_group_id__in=groups)
        return qs.order_by("plu")


class Warehouse(TenantModel):
    name = models.CharField("اسم المستودع", max_length=100)
    address = models.CharField("العنوان", max_length=255, blank=True)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "مستودع"
        verbose_name_plural = "المستودعات"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="warehouse")]

    def __str__(self):
        return self.name


class Batch(TenantModel):
    """تشغيلة (دفعة إنتاج/توريد) لصنف بتاريخ إنتاج وانتهاء."""

    item = models.ForeignKey(Item, models.PROTECT, related_name="batches", verbose_name="الصنف",
                             limit_choices_to={"has_batch_no": True})
    batch_no = models.CharField("رقم التشغيلة", max_length=40)
    manufacturing_date = models.DateField("تاريخ الإنتاج", null=True, blank=True)
    expiry_date = models.DateField("تاريخ الانتهاء", null=True, blank=True, db_index=True)
    reference = models.CharField("مصدر التشغيلة", max_length=60, blank=True, editable=False)
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        verbose_name = "تشغيلة"
        verbose_name_plural = "التشغيلات"
        ordering = ["expiry_date", "id"]
        constraints = [tenant_unique("item", "batch_no", model_label="batch")]

    def __str__(self):
        return f"{self.batch_no}" + (f" (ينتهي {self.expiry_date:%Y-%m-%d})" if self.expiry_date else "")

    def is_expired(self, on_date=None):
        from django.utils import timezone

        return bool(self.expiry_date and self.expiry_date < (on_date or timezone.localdate()))

    @property
    def days_left(self):
        from django.utils import timezone

        return (self.expiry_date - timezone.localdate()).days if self.expiry_date else None


class BatchBin(TenantModel):
    """رصيد التشغيلة في مستودع (يُحدَّث مع كل حركة مخزون)."""

    batch = models.ForeignKey(Batch, models.CASCADE, related_name="bins", verbose_name="التشغيلة")
    warehouse = models.ForeignKey("stock.Warehouse", models.CASCADE, related_name="+", verbose_name="المستودع")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=0)

    class Meta:
        verbose_name = "رصيد تشغيلة"
        verbose_name_plural = "أرصدة التشغيلات"
        constraints = [models.UniqueConstraint(fields=["batch", "warehouse"], name="unique_batch_bin")]


class Bin(TenantModel):
    """الرصيد الحالي لكل صنف في كل مستودع."""

    item = models.ForeignKey(Item, models.CASCADE, related_name="bins", verbose_name="الصنف")
    warehouse = models.ForeignKey(Warehouse, models.CASCADE, related_name="bins", verbose_name="المستودع")
    actual_qty = models.DecimalField("الكمية الفعلية", max_digits=18, decimal_places=3, default=0)
    valuation_rate = models.DecimalField("سعر التقييم", max_digits=18, decimal_places=6, default=0)
    stock_value = models.DecimalField("قيمة المخزون", max_digits=18, decimal_places=2, default=0)

    class Meta:
        verbose_name = "رصيد صنف"
        verbose_name_plural = "أرصدة الأصناف"
        unique_together = [("item", "warehouse")]


class StockLedgerEntry(TenantModel):
    posting_date = models.DateField("التاريخ", db_index=True)
    item = models.ForeignKey(Item, models.PROTECT, related_name="ledger_entries", verbose_name="الصنف")
    warehouse = models.ForeignKey(Warehouse, models.PROTECT, verbose_name="المستودع")
    actual_qty = models.DecimalField("الكمية (+ وارد / - صادر)", max_digits=18, decimal_places=3)
    incoming_rate = models.DecimalField("سعر الوارد", max_digits=18, decimal_places=6, default=0)
    valuation_rate = models.DecimalField("سعر التقييم", max_digits=18, decimal_places=6, default=0)
    qty_after_transaction = models.DecimalField("الرصيد بعد الحركة", max_digits=18, decimal_places=3)
    stock_value_difference = models.DecimalField("فرق القيمة", max_digits=18, decimal_places=2)
    voucher_type = models.CharField("نوع المستند", max_length=50)
    voucher_no = models.CharField("رقم المستند", max_length=40)
    voucher_id = models.PositiveBigIntegerField("معرّف المستند")
    batch = models.ForeignKey(Batch, models.PROTECT, null=True, blank=True, related_name="ledger_entries",
                              verbose_name="التشغيلة")
    is_cancelled = models.BooleanField("ملغى", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "حركة مخزون"
        verbose_name_plural = "دفتر حركة المخزون"
        ordering = ["posting_date", "id"]
        indexes = [models.Index(fields=["voucher_type", "voucher_id"]), models.Index(fields=["item", "warehouse"])]


class StockEntryPurpose(models.TextChoices):
    RECEIPT = "receipt", "إذن استلام (إدخال)"
    ISSUE = "issue", "إذن صرف (إخراج)"
    TRANSFER = "transfer", "تحويل بين المستودعات"


class StockEntry(SubmittableDocument):
    allow_expired_explicit = True  # إتلاف تشغيلة منتهية بذكر رقمها؛ الصرف التلقائي يتجاوز المنتهية

    naming_prefix = "STE"

    purpose = models.CharField("الغرض", max_length=20, choices=StockEntryPurpose.choices, default=StockEntryPurpose.RECEIPT)
    from_warehouse = models.ForeignKey(
        Warehouse, models.PROTECT, null=True, blank=True, related_name="+", verbose_name="من مستودع"
    )
    to_warehouse = models.ForeignKey(
        Warehouse, models.PROTECT, null=True, blank=True, related_name="+", verbose_name="إلى مستودع"
    )
    total_amount = models.DecimalField("إجمالي القيمة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "حركة مخزنية"
        verbose_name_plural = "الحركات المخزنية"

    def calculate_totals(self):
        total = 0
        for row in self.items.all():
            row.amount = flt(flt(row.qty, 3) * flt(row.rate))
            row.save(update_fields=["amount"])
            total += row.amount
        self.total_amount = flt(total)

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        if not rows:
            raise ValidationError("يجب إضافة صنف واحد على الأقل.")
        if self.purpose in (StockEntryPurpose.ISSUE, StockEntryPurpose.TRANSFER) and not self.from_warehouse_id:
            raise ValidationError("يرجى تحديد المستودع المصدر.")
        if self.purpose in (StockEntryPurpose.RECEIPT, StockEntryPurpose.TRANSFER) and not self.to_warehouse_id:
            raise ValidationError("يرجى تحديد المستودع الهدف.")
        if self.purpose == StockEntryPurpose.TRANSFER and self.from_warehouse_id == self.to_warehouse_id:
            raise ValidationError("لا يمكن التحويل إلى نفس المستودع.")
        for row in rows:
            if not row.item.is_stock_item:
                raise ValidationError(f"الصنف {row.item} ليس صنفاً مخزنياً.")
            if row.qty <= 0:
                raise ValidationError(f"الكمية للصنف {row.item} يجب أن تكون أكبر من صفر.")

    def on_submit(self):
        value_change = flt(0)
        for row in self.items.select_related("item"):
            if self.purpose == StockEntryPurpose.RECEIPT:
                value_change += make_stock_ledger_entry(self, row.item, self.to_warehouse, row.qty,
                                                        row.rate or row.item.buying_rate,
                                                        batch_no=row.batch_no, expiry_date=row.expiry_date)
            elif self.purpose == StockEntryPurpose.ISSUE:
                value_change += make_stock_ledger_entry(self, row.item, self.from_warehouse, -row.qty,
                                                        batch_no=row.batch_no)
            else:
                # التحويل ينقل نفس التشغيلات بنفس التكلفة إلى المستودع الهدف
                parts = []
                make_stock_ledger_entry(self, row.item, self.from_warehouse, -row.qty, batch_no=row.batch_no,
                                        allocations=parts)
                for batch, part, diff in parts:
                    make_stock_ledger_entry(self, row.item, self.to_warehouse, -part, -diff / -part if part else 0,
                                            batch_no=batch.batch_no if batch else "")
        if value_change:
            stock = self.company.require_account("default_stock_account")
            adjustment = self.company.require_account("default_stock_adjustment_account")
            make_gl_entries(self, [
                {"account": stock, "debit": value_change if value_change > 0 else 0, "credit": -value_change if value_change < 0 else 0},
                {"account": adjustment, "debit": -value_change if value_change < 0 else 0, "credit": value_change if value_change > 0 else 0},
            ], remarks=self.get_purpose_display())

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)


class OpeningStock(SubmittableDocument):
    """المخزون الافتتاحي: كميات وتكلفة الأصناف في مستودع، مقابل حساب الأرصدة الافتتاحية."""

    naming_prefix = "OPS"

    warehouse = models.ForeignKey(Warehouse, models.PROTECT, verbose_name="المستودع")
    total_amount = models.DecimalField("إجمالي القيمة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "مخزون افتتاحي"
        verbose_name_plural = "المخزون الافتتاحي"

    def calculate_totals(self):
        total = flt(0)
        for row in self.items.all():
            row.amount = flt(flt(row.qty, 3) * flt(row.rate))
            row.save(update_fields=["amount"])
            total += row.amount
        self.total_amount = total

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        if not rows:
            raise ValidationError("أضف صنفاً واحداً على الأقل.")
        for row in rows:
            if not row.item.is_stock_item:
                raise ValidationError(f"الصنف {row.item} ليس صنفاً مخزنياً.")
            if row.qty <= 0 or row.rate < 0:
                raise ValidationError(f"الصنف {row.item}: الكمية يجب أن تكون موجبة والتكلفة غير سالبة.")

    def on_submit(self):
        value = flt(0)
        for row in self.items.select_related("item"):
            value += make_stock_ledger_entry(self, row.item, self.warehouse, row.qty, row.rate,
                                             batch_no=row.batch_no, expiry_date=row.expiry_date)
        if value:
            make_gl_entries(self, [
                {"account": self.company.require_account("default_stock_account"), "debit": value},
                {"account": self.company.require_account("default_opening_account"), "credit": value},
            ], remarks=f"مخزون افتتاحي - {self.warehouse}")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)


class OpeningStockItem(models.Model):
    parent = models.ForeignKey(OpeningStock, models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, models.PROTECT, limit_choices_to={"disabled": False, "is_stock_item": True},
                             verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("تكلفة الوحدة", max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField("القيمة", max_digits=18, decimal_places=2, default=0, editable=False)

    batch_no = models.CharField("رقم التشغيلة", max_length=40, blank=True,
                                help_text="للأصناف المتتبعة: تشغيلة الوارد، أو التشغيلة المطلوب صرفها (فارغ = الأقرب انتهاءً)")
    expiry_date = models.DateField("تاريخ الانتهاء", null=True, blank=True)

    class Meta:
        verbose_name = "صنف افتتاحي"
        verbose_name_plural = "الأصناف"


class StockEntryItem(models.Model):
    parent = models.ForeignKey(StockEntry, models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0, help_text="يُستخدم في إذن الاستلام فقط")
    amount = models.DecimalField("القيمة", max_digits=18, decimal_places=2, default=0, editable=False)

    batch_no = models.CharField("رقم التشغيلة", max_length=40, blank=True,
                                help_text="للأصناف المتتبعة: تشغيلة الوارد، أو التشغيلة المطلوب صرفها (فارغ = الأقرب انتهاءً)")
    expiry_date = models.DateField("تاريخ الانتهاء", null=True, blank=True)

    class Meta:
        verbose_name = "بند حركة مخزنية"
        verbose_name_plural = "بنود الحركة"


class StockCount(SubmittableDocument):
    """الجرد الفعلي: مطابقة الكميات المعدودة مع الدفترية وترحيل الفروقات (زيادة أو عجز) لحساب تسويات المخزون."""

    naming_prefix = "CNT"
    allow_expired_batches = True  # عجز الجرد يُخصم من الأقرب انتهاءً حتى لو انتهت

    warehouse = models.ForeignKey(Warehouse, models.PROTECT, verbose_name="المستودع")
    item_group = models.ForeignKey(ItemGroup, models.PROTECT, null=True, blank=True, verbose_name="مجموعة الأصناف",
                                   help_text="اختياري: لجرد مجموعة واحدة. يُترك فارغاً لكل الأصناف")
    difference_value = models.DecimalField("صافي فرق القيمة", max_digits=18, decimal_places=2, default=0,
                                           editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "جرد مخزون"
        verbose_name_plural = "الجرد"

    def fetch_items(self):
        """يجلب أصناف المستودع (أو المجموعة) بالكمية الدفترية كقيمة مبدئية للمعدود."""
        items = Item.objects.filter(is_stock_item=True, disabled=False)
        if self.item_group_id:
            items = items.filter(item_group=self.item_group)
        balances = {b.item_id: b for b in Bin.objects.filter(warehouse=self.warehouse)}
        existing = set(self.items.values_list("item_id", flat=True))
        StockCountItem.objects.bulk_create([
            StockCountItem(parent=self, item=item, counted_qty=balances[item.id].actual_qty if item.id in balances else 0)
            # أصناف لها رصيد في المستودع، أو كل أصناف المجموعة عند تحديدها (لاكتشاف أصناف موجودة غير مسجلة)
            for item in items.order_by("code") if item.id not in existing
            and (self.item_group_id or (item.id in balances and balances[item.id].actual_qty))
        ])

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft and not self.items.exists():
            self.fetch_items()
        balances = {b.item_id: b for b in Bin.objects.filter(warehouse=self.warehouse)}
        total = flt(0)
        for row in self.items.select_related("item"):
            b = balances.get(row.item_id)
            if self.is_draft:
                row.system_qty = b.actual_qty if b else 0
                current_rate = b.valuation_rate if b and b.actual_qty else row.item.buying_rate
                row.valuation_rate = row.valuation_rate or current_rate
            row.difference_qty = flt(flt(row.counted_qty, 3) - flt(row.system_qty, 3), 3)
            row.difference_value = flt(row.difference_qty * row.valuation_rate)
            row.save(update_fields=["system_qty", "valuation_rate", "difference_qty", "difference_value"])
            total += row.difference_value
        self.difference_value = flt(total)

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        if not rows:
            raise ValidationError("لا توجد أصناف في الجرد.")
        seen = set()
        for row in rows:
            if row.item_id in seen:
                raise ValidationError(f"الصنف {row.item} مكرر في الجرد.")
            seen.add(row.item_id)
            if not row.item.is_stock_item:
                raise ValidationError(f"الصنف {row.item} ليس صنفاً مخزنياً.")
            if row.counted_qty < 0:
                raise ValidationError(f"الكمية المعدودة للصنف {row.item} لا تكون سالبة.")
        if not any(row.difference_qty for row in rows):
            raise ValidationError("لا توجد فروقات بين المعدود والدفتري؛ لا حاجة لاعتماد الجرد.")

    def on_submit(self):
        value_change = flt(0)
        for row in self.items.select_related("item"):
            if row.difference_qty > 0:
                # الزيادة تُضاف لأحدث تشغيلة موجودة في المستودع (أو تشغيلة جديدة إن لم توجد)
                from stock.batches import available_batches

                existing = available_batches(row.item, self.warehouse, self.posting_date, True) if row.item.has_batch_no else []
                value_change += make_stock_ledger_entry(self, row.item, self.warehouse, row.difference_qty,
                                                        row.valuation_rate,
                                                        batch_no=existing[-1][0].batch_no if existing else "")
            elif row.difference_qty < 0:
                value_change += make_stock_ledger_entry(self, row.item, self.warehouse, row.difference_qty)
        self.difference_value = value_change
        self.save(update_fields=["difference_value"])
        if value_change:
            stock = self.company.require_account("default_stock_account")
            adjustment = self.company.require_account("default_stock_adjustment_account")
            gain = value_change > 0
            make_gl_entries(self, [
                {"account": stock, "debit": value_change if gain else 0, "credit": 0 if gain else -value_change},
                {"account": adjustment, "debit": 0 if gain else -value_change, "credit": value_change if gain else 0},
            ], remarks=f"فروقات جرد {self.warehouse}")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)

    # ---------- الجرد بالماسح الضوئي ----------
    def scan(self, code, qty=1, user=None):
        """تسجيل قراءة ماسح: يعيد الصنف وإجمالي ما عُدّ منه في هذا الجرد."""
        from django.db.models import Sum

        if not self.is_draft:
            raise ValidationError("الجرد معتمد أو ملغى؛ لا يمكن المسح عليه.")
        item, scale_qty = Item.resolve_code(code)
        if item is None:
            raise ValidationError(f"لا يوجد صنف بهذا الباركود: {code}")
        if scale_qty is not None:  # باركود ميزان: الكمية = الوزن (مضروباً في عدد العبوات المدخل)
            qty = flt(scale_qty, 3) * flt(qty, 3)
        if not item.is_stock_item:
            raise ValidationError(f"«{item.name}» ليس صنفاً مخزنياً.")
        if self.item_group_id and item.item_group_id != self.item_group_id:
            raise ValidationError(f"«{item.name}» ليس من مجموعة هذا الجرد ({self.item_group}).")
        qty = flt(qty, 3)
        if qty == 0:
            raise ValidationError("الكمية لا تكون صفراً.")
        total = flt(self.scans.filter(item=item).aggregate(t=Sum("qty"))["t"] or 0, 3) + qty
        if total < 0:
            raise ValidationError("لا يمكن أن يصبح المعدود بالسالب.")
        StockCountScan.objects.create(parent=self, item=item, qty=qty, code=str(code)[:60], user=user)
        return item, total

    def scanned_totals(self):
        from django.db.models import Count, Max, Sum

        return list(self.scans.values("item_id", "item__code", "item__name", "item__barcode")
                    .annotate(qty=Sum("qty"), reads=Count("id"), last=Max("id")).order_by("-last"))

    def apply_scans(self, zero_unscanned=False):
        """نقل نتائج المسح إلى «الكمية المعدودة». zero_unscanned: الأصناف غير الممسوحة تُعتبر صفراً (جرد كامل)."""
        if not self.is_draft:
            raise ValidationError("الجرد معتمد أو ملغى.")
        totals = {row["item_id"]: row["qty"] for row in self.scanned_totals()}
        if not totals:
            raise ValidationError("لم يُمسح أي صنف بعد.")
        if not self.items.exists():
            self.fetch_items()
        rows = {row.item_id: row for row in self.items.all()}
        for item_id, qty in totals.items():
            row = rows.get(item_id) or StockCountItem(parent=self, item_id=item_id)
            row.counted_qty = flt(qty, 3)
            row.save()
            rows[item_id] = row
        if zero_unscanned:
            self.items.exclude(item_id__in=totals).update(counted_qty=0)
        self.calculate_totals()
        self.save()
        return len(totals)


class StockCountScan(models.Model):
    """سجل المسح بالماسح الضوئي أثناء الجرد: كل قراءة سطر، ويمكن لأكثر من شخص المسح معاً على أجهزة مختلفة."""

    parent = models.ForeignKey(StockCount, models.CASCADE, related_name="scans")
    item = models.ForeignKey(Item, models.PROTECT, related_name="+", verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    code = models.CharField("الكود الممسوح", max_length=60, blank=True)
    user = models.ForeignKey("auth.User", models.SET_NULL, null=True, related_name="+", verbose_name="بواسطة")
    created_at = models.DateTimeField("الوقت", auto_now_add=True)

    class Meta:
        verbose_name = "قراءة جرد"
        verbose_name_plural = "قراءات الجرد"
        ordering = ["id"]


class StockCountItem(models.Model):
    parent = models.ForeignKey(StockCount, models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, models.PROTECT, limit_choices_to={"disabled": False, "is_stock_item": True},
                             verbose_name="الصنف")
    counted_qty = models.DecimalField("الكمية المعدودة", max_digits=18, decimal_places=3, default=0)
    valuation_rate = models.DecimalField("تكلفة الوحدة", max_digits=18, decimal_places=6, default=0,
                                         help_text="تُستخدم لتقييم الزيادة؛ تؤخذ من متوسط التكلفة إن تُركت صفراً")
    system_qty = models.DecimalField("الكمية الدفترية", max_digits=18, decimal_places=3, default=0, editable=False)
    difference_qty = models.DecimalField("فرق الكمية", max_digits=18, decimal_places=3, default=0, editable=False)
    difference_value = models.DecimalField("فرق القيمة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "صنف جرد"
        verbose_name_plural = "أصناف الجرد"
