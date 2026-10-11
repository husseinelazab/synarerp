"""المستندات التجارية التي تسبق الفاتورة: عروض الأسعار وأوامر البيع والشراء.

لا ترحّل قيوداً ولا تحرّك المخزون؛ أثرها المحاسبي يحدث عند الفاتورة فقط.
الأوامر تتابع الكميات المفوترة من كل بند وتمنع الفوترة بأكثر من المطلوب.
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.invoice import CurrencyMixin, tax_totals
from core.models import SubmittableDocument
from core.utils import flt
from stock.uom import UOMLine, linked_qty


class CommercialStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    OPEN = "open", "مفتوح"
    ORDERED = "ordered", "تم التحويل"
    TO_BILL = "to_bill", "بانتظار الفوترة"
    PARTLY_BILLED = "partly_billed", "مفوتر جزئياً"
    BILLED = "billed", "مفوتر بالكامل"
    CLOSED = "closed", "مغلق"
    CANCELLED = "cancelled", "ملغى"


class CommercialDocument(CurrencyMixin, SubmittableDocument):
    posts_to_ledger = False
    party_field = "customer"
    submitted_status = CommercialStatus.OPEN

    currency = models.ForeignKey("core.Currency", models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="العملة")
    exchange_rate = models.DecimalField("سعر الصرف", max_digits=18, decimal_places=6, null=True, blank=True,
                                        help_text="يُترك فارغاً لاستخدام آخر سعر في جدول أسعار الصرف")
    tax_rate = models.DecimalField("نسبة الضريبة %", max_digits=5, decimal_places=2, default=15)
    prices_include_tax = models.BooleanField("الأسعار شاملة الضريبة", default=False)
    discount_amount = models.DecimalField("الخصم", max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField("الإجمالي قبل الخصم", max_digits=18, decimal_places=2, default=0, editable=False)
    net_total = models.DecimalField("الصافي قبل الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    tax_amount = models.DecimalField("مبلغ الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    grand_total = models.DecimalField("الإجمالي شامل الضريبة", max_digits=18, decimal_places=2, default=0, editable=False)
    base_grand_total = models.DecimalField("الإجمالي بعملة الشركة", max_digits=18, decimal_places=2, default=0,
                                           editable=False)
    terms = models.TextField("الشروط والأحكام", blank=True)
    status = models.CharField("الحالة", max_length=15, choices=CommercialStatus.choices,
                              default=CommercialStatus.DRAFT, editable=False)

    class Meta(SubmittableDocument.Meta):
        abstract = True

    @property
    def party(self):
        return getattr(self, self.party_field)

    @property
    def document_title(self):
        return self._meta.verbose_name

    def save(self, *args, **kwargs):
        self.ensure_currency()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        total = flt(0)
        for row in self.items.all():
            row.amount = flt(flt(row.qty, 3) * flt(row.rate))
            row.save(update_fields=["amount"])
            total += row.amount
        self.total = total
        self.net_total, self.tax_amount, self.grand_total = tax_totals(
            total, self.discount_amount, self.tax_rate, self.prices_include_tax)
        self.ensure_currency()
        self.base_grand_total = self.to_base(self.grand_total)

    def validate_for_submit(self):
        self.validate_currency()
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

    def on_submit(self):
        self.status = self.submitted_status
        self.save(update_fields=["status"])

    def on_cancel(self):
        self.status = CommercialStatus.CANCELLED
        self.save(update_fields=["status"])

    # ---------- التحويل إلى مستند آخر ----------
    def _copy_to(self, target_model, user, quantities, link_field=None, extra=None):
        """إنشاء مسودة من هذا المستند. quantities: {بند المصدر: الكمية}."""
        shared = {f.name for f in target_model._meta.concrete_fields}
        values = {self.party_field: self.party, "currency": self.currency, "exchange_rate": self.exchange_rate,
                  "tax_rate": self.tax_rate, "remarks": self.remarks}
        for name in ("prices_include_tax", "warehouse", "cost_center", "terms"):
            if name in shared and hasattr(self, name):
                values[name] = getattr(self, name)
        values = {k: v for k, v in values.items() if k in shared}
        if "warehouse" in shared and not values.get("warehouse"):
            # مثل الإدخال اليدوي: المستودع الافتراضي عند عدم تحديده في المستند المصدر
            from stock.models import Warehouse

            values["warehouse"] = Warehouse.objects.filter(disabled=False).first()
        values.update(extra or {})
        with transaction.atomic():
            target = target_model(company=self.company, created_by=user, **values)
            target.save()
            for row, qty in quantities.items():
                if qty <= 0:
                    continue
                line = {"item": row.item, "qty": qty, "rate": row.rate, "uom_id": row.uom_id}
                if link_field:
                    line[link_field] = row
                target.items.create(**line)
            if not target.items.exists():
                raise ValidationError("لا توجد كميات متبقية لتحويلها.")
            target.calculate_totals()
            # الخصم يُنقل بنسبة ما حُوّل من المستند
            if self.discount_amount and self.total and "discount_amount" in shared:
                target.discount_amount = flt(self.discount_amount * target.total / self.total)
                target.calculate_totals()
            target.save()
        return target


class CommercialItem(UOMLine):
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        abstract = True


class OrderDocument(CommercialDocument):
    """أمر بيع أو شراء: يتابع ما فُوتر من كل بند."""

    submitted_status = CommercialStatus.TO_BILL
    per_billed = models.DecimalField("نسبة الفوترة %", max_digits=6, decimal_places=2, default=0, editable=False)
    per_delivered = models.DecimalField("نسبة التسليم / الاستلام %", max_digits=6, decimal_places=2, default=0,
                                        editable=False)

    class Meta(CommercialDocument.Meta):
        abstract = True

    @property
    def has_deliveries(self):
        return self.items.filter(delivered_qty__gt=0).exists()

    def remaining_to_deliver(self):
        return {row: row.qty - row.delivered_qty for row in self.items.select_related("item")
                if row.item.is_stock_item and row.qty > row.delivered_qty}

    def update_delivery_status(self):
        rows = [r for r in self.items.select_related("item") if r.item.is_stock_item]
        ordered = sum((r.qty for r in rows), flt(0, 3))
        delivered = sum((min(r.delivered_qty, r.qty) for r in rows), flt(0, 3))
        self.per_delivered = flt(delivered * 100 / ordered) if ordered else flt(0)
        self.save(update_fields=["per_delivered"])

    def make_fulfillment(self, user=None):
        """إذن تسليم (لأمر البيع) أو استلام (لأمر الشراء) بالكميات المتبقية."""
        if not self.is_submitted or self.status in (CommercialStatus.CLOSED, CommercialStatus.CANCELLED):
            raise ValidationError("الأمر غير معتمد أو مغلق.")
        remaining = self.remaining_to_deliver()
        if not remaining:
            raise ValidationError("سُلّمت أو استُلمت كل الكميات المخزنية في هذا الأمر.")
        return self._copy_to(self.fulfillment_model(), user, remaining, link_field="order_item",
                             extra={"order": self})

    @property
    def is_open_for_billing(self):
        return self.is_submitted and self.status in (CommercialStatus.TO_BILL, CommercialStatus.PARTLY_BILLED)

    def remaining_quantities(self):
        return {row: row.qty - row.billed_qty for row in self.items.select_related("item") if row.qty > row.billed_qty}

    def update_billing_status(self):
        rows = list(self.items.all())
        ordered = sum((flt(r.qty * r.rate) for r in rows), flt(0))
        billed = sum((flt(min(r.billed_qty, r.qty) * r.rate) for r in rows), flt(0))
        self.per_billed = flt(billed * 100 / ordered) if ordered else flt(0)
        if self.status not in (CommercialStatus.CLOSED, CommercialStatus.CANCELLED):
            if all(r.billed_qty >= r.qty for r in rows):
                self.status = CommercialStatus.BILLED
            elif any(r.billed_qty > 0 for r in rows):
                self.status = CommercialStatus.PARTLY_BILLED
            else:
                self.status = CommercialStatus.TO_BILL
        self.save(update_fields=["per_billed", "status"])

    def before_cancel(self):
        if self.invoices.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الأمر لوجود فواتير معتمدة عليه. ألغِ الفواتير أولاً.")

    def close_order(self, user=None):
        if not self.is_open_for_billing:
            raise ValidationError("يمكن إغلاق الأوامر المفتوحة فقط.")
        self.status = CommercialStatus.CLOSED
        self.save(update_fields=["status"])

    def reopen_order(self, user=None):
        if self.status != CommercialStatus.CLOSED:
            raise ValidationError("الأمر غير مغلق.")
        self.status = CommercialStatus.TO_BILL
        self.update_billing_status()

    def make_invoice(self, user=None):
        if not self.is_open_for_billing:
            raise ValidationError("الأمر مفوتر بالكامل أو مغلق.")
        # إن سُلّمت/استُلمت البضاعة بأذون فالفاتورة لا تحرّك المخزون مرة أخرى
        extra = {"order": self, **({"update_stock": False} if self.has_deliveries else {})}
        return self._copy_to(self.invoice_model(), user, self.remaining_quantities(), link_field="order_item",
                             extra=extra)


class OrderItem(CommercialItem):
    billed_qty = models.DecimalField("الكمية المفوترة", max_digits=18, decimal_places=3, default=0, editable=False)
    delivered_qty = models.DecimalField("الكمية المسلّمة / المستلمة", max_digits=18, decimal_places=3, default=0,
                                        editable=False)

    class Meta:
        abstract = True


def update_order_billing(invoice, sign):
    """تحديث الكميات المفوترة في الأوامر المرتبطة بفاتورة عند اعتمادها (+1) أو إلغائها (-1)."""
    if invoice.is_return:
        return
    rows = [row for row in invoice.items.select_related("order_item") if row.order_item_id]
    totals = {}
    for row in rows:
        totals.setdefault(row.order_item_id, [row.order_item, flt(0, 3)])[1] += linked_qty(row, row.order_item)
    orders = set()
    for order_item, qty in totals.values():
        order_item.refresh_from_db()
        new_billed = order_item.billed_qty + sign * qty
        if sign > 0:
            order = order_item.parent
            if not order.is_open_for_billing:
                raise ValidationError(f"الأمر {order} مغلق أو مفوتر بالكامل.")
            if new_billed > order_item.qty:
                remaining = order_item.qty - order_item.billed_qty
                raise ValidationError(
                    f"الكمية المفوترة من «{order_item.item.name}» تتجاوز المتبقي في الأمر {order} ({remaining}).")
        order_item.billed_qty = max(new_billed, flt(0, 3))
        order_item.save(update_fields=["billed_qty"])
        orders.add(order_item.parent)
    for order in orders:
        order.update_billing_status()
