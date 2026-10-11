"""أساس مشترك لفواتير المبيعات والمشتريات ومرتجعاتها.

المرتجع (إشعار دائن للمبيعات / إشعار مدين للمشتريات) فاتورة من نفس النوع بعلامة is_return
وكميات ومبالغ سالبة مرتبطة بالفاتورة الأصلية، فتنعكس تلقائياً في كل التقارير والقيود.
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum

from core.models import SubmittableDocument
from core.utils import flt
from core.vat import REASON_CHOICES, VATCategory, validate_category
from stock.uom import UOMLine, linked_qty


def tax_totals(total, discount, rate, inclusive):
    """الصافي قبل الضريبة والضريبة والإجمالي، لأسعار شاملة أو غير شاملة للضريبة."""
    if inclusive:
        grand = flt(total - flt(discount))
        tax = flt(grand * flt(rate) / (100 + flt(rate)))
        return grand - tax, tax, grand
    net = flt(total - flt(discount))
    tax = flt(net * flt(rate) / 100)
    return net, tax, flt(net + tax)


class CurrencyMixin:
    """عملة المستند وسعر صرفه؛ القيود تُسجل دائماً بعملة الشركة."""

    def ensure_currency(self):
        from core.models import Company, ExchangeRate

        company = self.company if self.company_id else Company.current()
        if company is None:
            return
        if not self.currency_id:
            self.currency_id = company.currency_id
        if self.currency_id == company.currency_id:
            self.exchange_rate = 1
        elif not self.exchange_rate:
            self.exchange_rate = ExchangeRate.lookup(company, self.currency, self.posting_date)

    @property
    def is_foreign_currency(self):
        return bool(self.currency_id) and self.currency_id != self.company.currency_id

    def validate_currency(self):
        self.ensure_currency()
        if not self.exchange_rate or self.exchange_rate <= 0:
            raise ValidationError(
                f"أدخل سعر صرف {self.currency} في المستند، أو أضفه في «الإعدادات ← أسعار الصرف».")

    def to_base(self, amount):
        return flt(flt(amount) * (self.exchange_rate or 1))


class InvoiceDocument(CurrencyMixin, SubmittableDocument):
    due_date = models.DateField("تاريخ الاستحقاق", null=True, blank=True)
    update_stock = models.BooleanField("تحديث المخزون", default=True)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="المستودع")
    currency = models.ForeignKey("core.Currency", models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="العملة")
    exchange_rate = models.DecimalField("سعر الصرف", max_digits=18, decimal_places=6, null=True, blank=True,
                                        help_text="يُترك فارغاً لاستخدام آخر سعر في جدول أسعار الصرف")
    tax_rate = models.DecimalField("نسبة الضريبة %", max_digits=5, decimal_places=2, default=15)
    vat_category = models.CharField("فئة الضريبة", max_length=1, choices=VATCategory.choices,
                                    default=VATCategory.STANDARD,
                                    help_text="تُحدد تلقائياً من الأصناف إن كانت كلها صفرية أو معفاة")
    vat_exemption_reason = models.CharField("سبب الإعفاء / النسبة الصفرية", max_length=20, choices=REASON_CHOICES,
                                            blank=True)
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False},
                                    verbose_name="مركز التكلفة / الفرع")
    total = models.DecimalField("الإجمالي قبل الخصم", max_digits=18, decimal_places=2, default=0, editable=False)
    discount_amount = models.DecimalField("الخصم", max_digits=18, decimal_places=2, default=0)
    net_total = models.DecimalField("الصافي قبل الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    tax_amount = models.DecimalField("مبلغ الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    grand_total = models.DecimalField("الإجمالي شامل الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    outstanding_amount = models.DecimalField("المبلغ المتبقي", max_digits=18, decimal_places=2, default=0, editable=False)
    base_net_total = models.DecimalField("الصافي بعملة الشركة", max_digits=18, decimal_places=2, default=0, editable=False)
    base_tax_amount = models.DecimalField("الضريبة بعملة الشركة", max_digits=18, decimal_places=2, default=0, editable=False)
    base_grand_total = models.DecimalField("الإجمالي بعملة الشركة", max_digits=18, decimal_places=2, default=0, editable=False)

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
    return_locked_fields = ["tax_rate", "vat_category", "vat_exemption_reason", "update_stock", "cost_center", "currency",
                            "exchange_rate"]

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
        """إجمالي الكمية (بالوحدة الأساسية) والمبلغ لكل صنف في الفاتورة: {item_id: {"qty", "amount"}}."""
        summary = {}
        for row in self.items.all():
            entry = summary.setdefault(row.item_id, {"qty": flt(0, 3), "amount": flt(0)})
            entry["qty"] += row.stock_qty
            entry["amount"] += row.amount
        return summary

    def returned_quantities(self):
        rows = (
            self.items.model.objects.filter(parent__return_against=self, parent__docstatus=1)
            .values("item_id").annotate(qty=Sum("stock_qty"))
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
                # المرتجع دائماً بالوحدة الأساسية وسعرها في الفاتورة الأصلية
                row.qty = -abs(row.stock_qty if row.uom_id else row.qty)
                row.uom_id = None
                row.rate = rates.get(row.item_id, row.rate)
            row.amount = flt(flt(row.qty, 3) * flt(row.rate))
            row.save(update_fields=["qty", "rate", "amount"])
            total += row.amount
        self.total = total
        if not original and self.is_draft:
            self.apply_item_vat_category()
        if self.vat_category != VATCategory.STANDARD:
            self.tax_rate = 0
        elif not flt(self.tax_rate) and self.company_id and self.company.default_tax_rate:
            self.tax_rate = self.company.default_tax_rate  # عاد للفئة الأساسية بعد أن كانت صفرية
        if original:
            # الخصم في المرتجع يُحسب تلقائياً بنسبة ما أُرجع من الفاتورة الأصلية
            self.discount_amount = flt(original.discount_amount * total / original.total) if original.total else flt(0)
        elif getattr(self, "order_id", None) and self.order.discount_amount and self.order.total:
            # الفاتورة المرتبطة بأمر تأخذ من خصمه بنسبة قيمة بنود الأمر التي تفوترها، فلا يتكرر الخصم
            billed = sum((flt(r.qty * r.rate) for r in self.items.all() if r.order_item_id), flt(0))
            self.discount_amount = flt(self.order.discount_amount * billed / self.order.total)
        self.net_total, self.tax_amount, self.grand_total = tax_totals(
            total, self.discount_amount, self.tax_rate, self.prices_include_tax)
        if self.is_draft:
            self.outstanding_amount = self.grand_total
        self.ensure_currency()
        self.base_net_total = self.to_base(self.net_total)
        self.base_tax_amount = self.to_base(self.tax_amount)
        self.base_grand_total = self.base_net_total + self.base_tax_amount

    def apply_item_vat_category(self):
        """إن كانت كل الأصناف بفئة واحدة غير أساسية (صفرية/معفاة) تُطبق على الفاتورة مع سببها."""
        items = [row.item for row in self.items.select_related("item")]
        categories = {item.vat_category for item in items}
        if len(categories) == 1 and self.vat_category == VATCategory.STANDARD:
            category = categories.pop()
            if category != VATCategory.STANDARD:
                self.vat_category = category
                self.vat_exemption_reason = self.vat_exemption_reason or items[0].vat_exemption_reason

    def validate_vat(self, rows):
        error = validate_category(self.vat_category, self.vat_exemption_reason, "الفاتورة")
        if error:
            raise ValidationError(error)
        if self.vat_category == VATCategory.STANDARD and flt(self.tax_rate) <= 0:
            raise ValidationError("الفئة الأساسية تتطلب نسبة ضريبة أكبر من صفر؛ اختر الفئة الصفرية أو المعفاة إن لزم.")
        mixed = sorted({row.item.vat_category for row in rows} - {self.vat_category})
        if mixed:
            names = "، ".join(row.item.name for row in rows if row.item.vat_category != self.vat_category)
            raise ValidationError(
                f"أصناف بفئة ضريبية مختلفة عن الفاتورة ({names}). افصلها في فاتورة مستقلة لكل فئة "
                "(الفوترة الإلكترونية تتطلب فئة واحدة صحيحة لكل بند).")

    def save(self, *args, **kwargs):
        self.ensure_currency()
        super().save(*args, **kwargs)

    def validate_for_submit(self):
        self.validate_currency()
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
        self.validate_vat(rows)
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
            requested[row.item_id] = requested.get(row.item_id, 0) - row.stock_qty
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


class InvoiceItem(UOMLine):
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        abstract = True
