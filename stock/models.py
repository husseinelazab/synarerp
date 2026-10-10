from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.models import SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt
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
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "صنف"
        verbose_name_plural = "الأصناف"
        ordering = ["code"]
        constraints = [tenant_unique("code", model_label="item")]

    def __str__(self):
        return f"{self.code} - {self.name}"


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
