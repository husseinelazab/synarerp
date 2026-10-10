"""أساس مشترك لفواتير المبيعات والمشتريات."""
from django.core.exceptions import ValidationError
from django.db import models

from core.models import SubmittableDocument
from core.utils import flt


class InvoiceDocument(SubmittableDocument):
    due_date = models.DateField("تاريخ الاستحقاق", null=True, blank=True)
    update_stock = models.BooleanField("تحديث المخزون", default=True)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="المستودع")
    tax_rate = models.DecimalField("نسبة الضريبة %", max_digits=5, decimal_places=2, default=15)
    total = models.DecimalField("الإجمالي قبل الخصم", max_digits=18, decimal_places=2, default=0, editable=False)
    discount_amount = models.DecimalField("الخصم", max_digits=18, decimal_places=2, default=0)
    net_total = models.DecimalField("الصافي قبل الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    tax_amount = models.DecimalField("مبلغ الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    grand_total = models.DecimalField("الإجمالي شامل الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    outstanding_amount = models.DecimalField("المبلغ المتبقي", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        abstract = True

    @property
    def paid_amount(self):
        return flt(self.grand_total - self.outstanding_amount)

    def calculate_totals(self):
        total = flt(0)
        for row in self.items.all():
            row.amount = flt(flt(row.qty, 3) * flt(row.rate))
            row.save(update_fields=["amount"])
            total += row.amount
        self.total = total
        self.net_total = flt(total - flt(self.discount_amount))
        self.tax_amount = flt(self.net_total * flt(self.tax_rate) / 100)
        self.grand_total = flt(self.net_total + self.tax_amount)
        if self.is_draft:
            self.outstanding_amount = self.grand_total

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        if not rows:
            raise ValidationError("يجب إضافة صنف واحد على الأقل.")
        for row in rows:
            if row.qty <= 0:
                raise ValidationError(f"الكمية للصنف {row.item} يجب أن تكون أكبر من صفر.")
            if row.rate < 0:
                raise ValidationError(f"السعر للصنف {row.item} لا يمكن أن يكون سالباً.")
        if self.discount_amount < 0 or self.discount_amount > self.total:
            raise ValidationError("قيمة الخصم غير صحيحة.")
        if self.update_stock and any(r.item.is_stock_item for r in rows) and not self.warehouse_id:
            raise ValidationError("يرجى تحديد المستودع لتحديث المخزون.")
        if self.due_date and self.due_date < self.posting_date:
            raise ValidationError("تاريخ الاستحقاق لا يمكن أن يسبق تاريخ الفاتورة.")

    def before_cancel(self):
        if self.payments.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الفاتورة لوجود سندات مرتبطة بها. ألغِ السندات أولاً.")


class InvoiceItem(models.Model):
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        abstract = True
