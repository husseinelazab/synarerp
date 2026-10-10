"""أساس مشترك لفواتير المبيعات والمشتريات ومرتجعاتها.

المرتجع (إشعار دائن للمبيعات / إشعار مدين للمشتريات) فاتورة من نفس النوع بعلامة is_return
وكميات ومبالغ سالبة مرتبطة بالفاتورة الأصلية، فتنعكس تلقائياً في كل التقارير والقيود.
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum

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

    is_return = models.BooleanField("مرتجع", default=False, editable=False)
    return_against = models.ForeignKey(
        "self", models.PROTECT, null=True, blank=True, related_name="returns", editable=False, verbose_name="مرتجع من الفاتورة"
    )
    applied_to_original = models.DecimalField(
        "المخصوم من رصيد الفاتورة الأصلية", max_digits=18, decimal_places=2, default=0, editable=False
    )

    # فواتير المبيعات تعيد تعريفه كحقل؛ المشتريات تعمل دائماً بأسعار غير شاملة للضريبة
    prices_include_tax = False
    party_field = ""
    return_prefix = "RET"
    # حقول تُنسخ من الفاتورة الأصلية إلى المرتجع ولا يجوز تغييرها فيه
    return_locked_fields = ["tax_rate", "update_stock"]

    class Meta(SubmittableDocument.Meta):
        abstract = True

    def get_naming_prefix(self):
        return self.return_prefix if self.is_return else self.naming_prefix

    @property
    def party(self):
        return getattr(self, self.party_field)

    @property
    def discount_excluding_tax(self):
        """قيمة الخصم قبل الضريبة (عند الأسعار الشاملة يُستخرج منها جزء الضريبة)."""
        if not self.prices_include_tax:
            return flt(self.discount_amount)
        return flt(flt(self.discount_amount) * 100 / (100 + flt(self.tax_rate)))

    @property
    def paid_amount(self):
        return flt(self.grand_total - self.outstanding_amount)

    # ---------- بيانات المرتجعات ----------

    def item_summary(self):
        """إجمالي الكمية والمبلغ لكل صنف في الفاتورة: {item_id: {"qty", "amount"}}."""
        summary = {}
        for row in self.items.all():
            entry = summary.setdefault(row.item_id, {"qty": flt(0, 3), "amount": flt(0)})
            entry["qty"] += row.qty
            entry["amount"] += row.amount
        return summary

    def returned_quantities(self):
        rows = (
            self.items.model.objects.filter(parent__return_against=self, parent__docstatus=1)
            .values("item_id").annotate(qty=Sum("qty"))
        )
        return {r["item_id"]: -flt(r["qty"], 3) for r in rows}

    def returnable_quantities(self):
        returned = self.returned_quantities()
        return {
            item_id: data["qty"] - returned.get(item_id, 0)
            for item_id, data in self.item_summary().items()
            if data["qty"] - returned.get(item_id, 0) > 0
        }

    def make_return(self, user=None, quantities=None):
        """إنشاء مسودة مرتجع. quantities: {item_id: كمية موجبة}، والافتراضي كل المتبقي القابل للإرجاع."""
        if not self.is_submitted or self.is_return:
            raise ValidationError("يمكن إنشاء مرتجع من فاتورة معتمدة فقط.")
        remaining = self.returnable_quantities()
        if quantities is None:
            quantities = remaining
        summary = self.item_summary()
        with transaction.atomic():
            ret = type(self)(
                company=self.company, is_return=True, return_against=self, created_by=user,
                warehouse=self.warehouse, **{f: getattr(self, f) for f in self.return_locked_fields},
            )
            ret.save()
            for item_id, qty in quantities.items():
                qty = flt(qty, 3)
                if qty <= 0:
                    continue
                if item_id not in summary:
                    raise ValidationError("لا يمكن إرجاع صنف غير موجود في الفاتورة الأصلية.")
                data = summary[item_id]
                ret.items.create(item_id=item_id, qty=-qty, rate=flt(data["amount"] / data["qty"]))
            if not ret.items.exists():
                raise ValidationError("لا توجد كميات قابلة للإرجاع في هذه الفاتورة.")
            ret.calculate_totals()
            ret.save()
        return ret

    # ---------- الحسابات ----------

    def calculate_totals(self):
        original = self.return_against if self.is_return and self.return_against_id else None
        rates = {}
        if original:
            for field in self.return_locked_fields:
                setattr(self, field, getattr(original, field))
            rates = {i: flt(d["amount"] / d["qty"]) for i, d in original.item_summary().items() if d["qty"]}
        total = flt(0)
        for row in self.items.all():
            if self.is_return:
                row.qty = -abs(row.qty)
                row.rate = rates.get(row.item_id, row.rate)
            row.amount = flt(flt(row.qty, 3) * flt(row.rate))
            row.save(update_fields=["qty", "rate", "amount"])
            total += row.amount
        self.total = total
        if original:
            # الخصم في المرتجع يُحسب تلقائياً بنسبة ما أُرجع من الفاتورة الأصلية
            self.discount_amount = flt(original.discount_amount * total / original.total) if original.total else flt(0)
        if self.prices_include_tax:
            self.grand_total = flt(total - flt(self.discount_amount))
            self.tax_amount = flt(self.grand_total * flt(self.tax_rate) / (100 + flt(self.tax_rate)))
            self.net_total = self.grand_total - self.tax_amount
        else:
            self.net_total = flt(total - flt(self.discount_amount))
            self.tax_amount = flt(self.net_total * flt(self.tax_rate) / 100)
            self.grand_total = flt(self.net_total + self.tax_amount)
        if self.is_draft:
            self.outstanding_amount = self.grand_total

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        if not rows:
            raise ValidationError("يجب إضافة صنف واحد على الأقل.")
        sign = -1 if self.is_return else 1
        for row in rows:
            if row.qty * sign <= 0:
                raise ValidationError(f"الكمية للصنف {row.item} يجب أن تكون أكبر من صفر.")
            if row.rate < 0:
                raise ValidationError(f"السعر للصنف {row.item} لا يمكن أن يكون سالباً.")
        if self.discount_amount * sign < 0 or abs(self.discount_amount) > abs(self.total):
            raise ValidationError("قيمة الخصم غير صحيحة.")
        if self.update_stock and any(r.item.is_stock_item for r in rows) and not self.warehouse_id:
            raise ValidationError("يرجى تحديد المستودع لتحديث المخزون.")
        if self.due_date and self.due_date < self.posting_date:
            raise ValidationError("تاريخ الاستحقاق لا يمكن أن يسبق تاريخ الفاتورة.")
        if self.is_return:
            self._validate_return(rows)

    def _validate_return(self, rows):
        original = self.return_against
        if original is None or not original.is_submitted:
            raise ValidationError("المرتجع يجب أن يرتبط بفاتورة أصلية معتمدة.")
        if getattr(original, f"{self.party_field}_id") != getattr(self, f"{self.party_field}_id"):
            raise ValidationError("طرف المرتجع يجب أن يطابق طرف الفاتورة الأصلية.")
        remaining = original.returnable_quantities()
        requested = {}
        for row in rows:
            requested[row.item_id] = requested.get(row.item_id, 0) - row.qty
        for row in rows:
            available = remaining.get(row.item_id, 0)
            if requested[row.item_id] > available:
                raise ValidationError(
                    f"الكمية المرتجعة من «{row.item.name}» ({requested[row.item_id]}) تتجاوز المتاح للإرجاع ({available})."
                )

    # ---------- أثر المرتجع على رصيد الفاتورة الأصلية ----------

    def apply_return_to_original(self):
        """يُخصم المرتجع من المتبقي على الفاتورة الأصلية، وما يزيد يبقى رصيداً مستحق الرد على المرتجع."""
        if not (self.is_return and self.return_against_id):
            return
        original = type(self).objects.select_for_update().get(pk=self.return_against_id)
        applied = min(max(original.outstanding_amount, flt(0)), -self.grand_total)
        original.outstanding_amount = flt(original.outstanding_amount - applied)
        original.save(update_fields=["outstanding_amount"])
        self.applied_to_original = applied
        self.outstanding_amount = flt(self.grand_total + applied)
        self.save(update_fields=["applied_to_original", "outstanding_amount"])

    def unapply_return_from_original(self):
        if self.is_return and self.return_against_id and self.applied_to_original:
            original = type(self).objects.select_for_update().get(pk=self.return_against_id)
            original.outstanding_amount = flt(original.outstanding_amount + self.applied_to_original)
            original.save(update_fields=["outstanding_amount"])

    def before_cancel(self):
        if self.payments.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الفاتورة لوجود سندات مرتبطة بها. ألغِ السندات أولاً.")
        if self.returns.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الفاتورة لوجود مرتجعات معتمدة عليها. ألغِ المرتجعات أولاً.")


class InvoiceItem(models.Model):
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        abstract = True
