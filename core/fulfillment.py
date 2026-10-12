"""أذون التسليم (للمبيعات) والاستلام (للمشتريات): حركة البضاعة منفصلة عن الفاتورة.

- إذن التسليم يُخرج البضاعة ويقيّد تكلفتها (مدين تكلفة المبيعات / دائن المخزون)، وفاتورته لا تحرّك المخزون.
- إذن الاستلام يُدخل البضاعة (مدين المخزون / دائن «بضائع مستلمة لم تصل فواتيرها»)، وفاتورة المورد تُقفل الحساب الوسيط.
- الأوامر تتابع الكميات المسلّمة أو المستلمة، والأذون تتابع ما فُوتر منها.
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.invoice import CurrencyMixin
from core.models import SubmittableDocument
from core.utils import flt
from stock.uom import UOMLine, linked_qty

SRBNB_ACCOUNT = "بضائع مستلمة لم تصل فواتيرها"


class FulfillmentStatus(models.TextChoices):
    TO_BILL = "to_bill", "بانتظار الفوترة"
    PARTLY_BILLED = "partly_billed", "مفوتر جزئياً"
    BILLED = "billed", "مفوتر بالكامل"


class FulfillmentDocument(CurrencyMixin, SubmittableDocument):
    party_field = ""
    invoice_link_field = ""  # اسم حقل بند الفاتورة الذي يشير لبند الإذن

    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+", verbose_name="المستودع")
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False},
                                    verbose_name="مركز التكلفة / الفرع")
    currency = models.ForeignKey("core.Currency", models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="العملة")
    exchange_rate = models.DecimalField("سعر الصرف", max_digits=18, decimal_places=6, null=True, blank=True,
                                        help_text="يُترك فارغاً لاستخدام آخر سعر في جدول أسعار الصرف")
    tax_rate = models.DecimalField("نسبة الضريبة % (للفاتورة)", max_digits=5, decimal_places=2, default=15)
    total_amount = models.DecimalField("الإجمالي (قبل الضريبة)", max_digits=18, decimal_places=2, default=0,
                                       editable=False)
    per_billed = models.DecimalField("نسبة الفوترة %", max_digits=6, decimal_places=2, default=0, editable=False)
    status = models.CharField("الحالة", max_length=15, choices=FulfillmentStatus.choices,
                              default=FulfillmentStatus.TO_BILL, editable=False)

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
        self.total_amount = total
        self.ensure_currency()

    def validate_for_submit(self):
        self.validate_currency()
        rows = list(self.items.select_related("item", "order_item", "order_item__parent"))
        if not rows:
            raise ValidationError("يجب إضافة صنف واحد على الأقل.")
        for row in rows:
            if not row.item.is_stock_item:
                raise ValidationError(f"الصنف {row.item} خدمي لا يحتاج إذناً؛ فوتره مباشرة.")
            if row.qty <= 0:
                raise ValidationError(f"الكمية للصنف {row.item} يجب أن تكون أكبر من صفر.")
            if row.rate < 0:
                raise ValidationError(f"السعر للصنف {row.item} لا يمكن أن يكون سالباً.")
            if row.order_item_id:
                order = row.order_item.parent
                if getattr(order, f"{self.party_field}_id") != getattr(self, f"{self.party_field}_id"):
                    raise ValidationError(f"طرف الإذن لا يطابق طرف الأمر {order}.")
        # فحص الكميات مقابل الأمر قبل تحريك المخزون
        requested = {}
        for row in rows:
            if row.order_item_id:
                requested[row.order_item_id] = requested.get(row.order_item_id, 0) + linked_qty(row, row.order_item)
        for row in rows:
            if row.order_item_id:
                remaining = row.order_item.qty - row.order_item.delivered_qty
                if requested[row.order_item_id] > remaining:
                    raise ValidationError(f"الكمية من «{row.item.name}» تتجاوز المتبقي في الأمر "
                                          f"{row.order_item.parent} ({remaining}).")

    # ---------- متابعة الأوامر ----------
    def update_order_delivery(self, sign):
        totals = {}
        for row in self.items.select_related("order_item"):
            if row.order_item_id:
                totals.setdefault(row.order_item_id, [row.order_item, flt(0, 3)])[1] += linked_qty(row, row.order_item)
        orders = set()
        for order_item, qty in totals.values():
            order_item.refresh_from_db()
            new_qty = order_item.delivered_qty + sign * qty
            order = order_item.parent
            if sign > 0:
                if not order.is_submitted or order.status == "closed":
                    raise ValidationError(f"الأمر {order} غير معتمد أو مغلق.")
                if new_qty > order_item.qty:
                    raise ValidationError(f"الكمية من «{order_item.item.name}» تتجاوز المتبقي في الأمر {order} "
                                          f"({order_item.qty - order_item.delivered_qty}).")
            order_item.delivered_qty = max(new_qty, flt(0, 3))
            order_item.save(update_fields=["delivered_qty"])
            orders.add(order)
        for order in orders:
            order.update_delivery_status()

    # ---------- الفوترة من الإذن ----------
    def remaining_to_bill(self):
        return {row: row.qty - row.billed_qty for row in self.items.select_related("item", "order_item")
                if row.qty > row.billed_qty}

    @property
    def available_actions(self):
        if self.is_submitted and self.status != FulfillmentStatus.BILLED:
            return [("make_invoice", f"إنشاء {self.invoice_model()._meta.verbose_name}")]
        return []

    def make_invoice(self, user=None):
        remaining = self.remaining_to_bill()
        if not self.is_submitted or not remaining:
            raise ValidationError("لا توجد كميات متبقية للفوترة في هذا الإذن.")
        model = self.invoice_model()
        orders = {row.order_item.parent for row in remaining if row.order_item_id}
        values = {self.party_field: self.party, "currency": self.currency, "exchange_rate": self.exchange_rate,
                  "tax_rate": self.tax_rate, "warehouse": self.warehouse, "cost_center": self.cost_center,
                  "update_stock": False, "remarks": f"فاتورة {self._meta.verbose_name} {self.name}",
                  "order": orders.pop() if len(orders) == 1 else None}
        with transaction.atomic():
            invoice = model.objects.create(company=self.company, created_by=user, **values)
            for row, qty in remaining.items():
                invoice.items.create(item=row.item, qty=qty, rate=row.rate, uom_id=row.uom_id,
                                     order_item=row.order_item, **{self.invoice_link_field: row})
            invoice.calculate_totals()
            invoice.save()
        return invoice

    def update_billing_status(self):
        rows = list(self.items.all())
        value = sum((flt(r.qty * r.rate) for r in rows), flt(0))
        billed = sum((flt(min(r.billed_qty, r.qty) * r.rate) for r in rows), flt(0))
        self.per_billed = flt(billed * 100 / value) if value else (flt(100) if all(r.billed_qty >= r.qty for r in rows)
                                                                   else flt(0))
        if all(r.billed_qty >= r.qty for r in rows):
            self.status = FulfillmentStatus.BILLED
        elif any(r.billed_qty > 0 for r in rows):
            self.status = FulfillmentStatus.PARTLY_BILLED
        else:
            self.status = FulfillmentStatus.TO_BILL
        self.save(update_fields=["per_billed", "status"])

    def before_cancel(self):
        if self.items.filter(billed_qty__gt=0).exists():
            raise ValidationError("فُوتر جزء من هذا الإذن؛ ألغِ الفواتير المرتبطة أولاً.")


class FulfillmentItem(UOMLine):
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False, "is_stock_item": True},
                             verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    serial_nos = models.TextField("الأرقام التسلسلية", blank=True,
                                  help_text="للأصناف المتسلسلة: رقم لكل قطعة، مفصولة بسطر أو فاصلة")
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0, editable=False)
    billed_qty = models.DecimalField("الكمية المفوترة", max_digits=18, decimal_places=3, default=0, editable=False)

    class Meta:
        abstract = True


def update_fulfillment_billing(invoice, link_field, sign):
    """تحديث الكميات المفوترة في بنود الأذون عند اعتماد فاتورة (+1) أو إلغائها (-1)."""
    if invoice.is_return:
        return
    totals = {}
    for row in invoice.items.select_related(link_field):
        linked = getattr(row, link_field)
        if linked is not None:
            totals.setdefault(linked.pk, [linked, flt(0, 3)])[1] += linked_qty(row, linked)
    docs = set()
    for line, qty in totals.values():
        line.refresh_from_db()
        new_qty = line.billed_qty + sign * qty
        if sign > 0 and new_qty > line.qty:
            raise ValidationError(f"الكمية المفوترة من «{line.item.name}» تتجاوز المتبقي في الإذن {line.parent} "
                                  f"({line.qty - line.billed_qty}).")
        line.billed_qty = max(new_qty, flt(0, 3))
        line.save(update_fields=["billed_qty"])
        docs.add(line.parent)
    for doc in docs:
        doc.update_billing_status()


def srbnb_account(company):
    from accounts.models import Account

    account = Account.objects.filter(name=SRBNB_ACCOUNT, is_group=False).first()
    if account is None:
        raise ValidationError(f"أضف حساب «{SRBNB_ACCOUNT}» (خصوم متداولة) في دليل الحسابات.")
    return account
