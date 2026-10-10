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
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "صنف"
        verbose_name_plural = "الأصناف"
        ordering = ["code"]
        constraints = [tenant_unique("code", model_label="item")]

    def __str__(self):
        return f"{self.code} - {self.name}"

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
                value_change += make_stock_ledger_entry(self, row.item, self.to_warehouse, row.qty, row.rate or row.item.buying_rate)
            elif self.purpose == StockEntryPurpose.ISSUE:
                value_change += make_stock_ledger_entry(self, row.item, self.from_warehouse, -row.qty)
            else:
                out_value = make_stock_ledger_entry(self, row.item, self.from_warehouse, -row.qty)
                rate = -out_value / row.qty
                make_stock_ledger_entry(self, row.item, self.to_warehouse, row.qty, rate)
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
            value += make_stock_ledger_entry(self, row.item, self.warehouse, row.qty, row.rate)
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

    class Meta:
        verbose_name = "صنف افتتاحي"
        verbose_name_plural = "الأصناف"


class StockEntryItem(models.Model):
    parent = models.ForeignKey(StockEntry, models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0, help_text="يُستخدم في إذن الاستلام فقط")
    amount = models.DecimalField("القيمة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "بند حركة مخزنية"
        verbose_name_plural = "بنود الحركة"


class StockCount(SubmittableDocument):
    """الجرد الفعلي: مطابقة الكميات المعدودة مع الدفترية وترحيل الفروقات (زيادة أو عجز) لحساب تسويات المخزون."""

    naming_prefix = "CNT"

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
                value_change += make_stock_ledger_entry(self, row.item, self.warehouse, row.difference_qty,
                                                        row.valuation_rate)
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
        item = Item.find_by_code(code)
        if item is None:
            raise ValidationError(f"لا يوجد صنف بهذا الباركود: {code}")
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
