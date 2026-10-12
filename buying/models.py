from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.commercial import (CommercialDocument, CommercialItem, CommercialStatus, OrderDocument, OrderItem,
                             update_order_billing)
from core.fulfillment import FulfillmentDocument, FulfillmentItem, srbnb_account, update_fulfillment_billing
from core.invoice import InvoiceDocument, InvoiceItem
from core.models import SubmittableDocument, TenantModel
from core.utils import flt
from core.tenancy import tenant_unique
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry, make_value_adjustment
from stock.pricing import update_buying_rates
from stock.uom import linked_qty


class Supplier(TenantModel):
    name = models.CharField("اسم المورد", max_length=200)
    tax_id = models.CharField("الرقم الضريبي", max_length=20, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=100, blank=True)
    address = models.TextField("العنوان", blank=True)
    price_list = models.ForeignKey("pricing.PriceList", models.SET_NULL, null=True, blank=True, related_name="+",
                                   limit_choices_to={"kind": "buying", "disabled": False},
                                   verbose_name="قائمة أسعار المورد")
    payment_terms = models.ForeignKey("accounts.PaymentTermsTemplate", models.SET_NULL, null=True, blank=True,
                                      related_name="+", verbose_name="شروط الدفع الافتراضية")
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "مورد"
        verbose_name_plural = "الموردون"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="supplier")]

    def __str__(self):
        return self.name


class PurchaseOrder(OrderDocument):
    naming_prefix = "PO"
    party_field = "supplier"

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    schedule_date = models.DateField("تاريخ التوريد المتوقع", null=True, blank=True)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+",
                                  verbose_name="مستودع الاستلام")
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False},
                                    verbose_name="مركز التكلفة / الفرع")

    class Meta(OrderDocument.Meta):
        verbose_name = "أمر شراء"
        verbose_name_plural = "أوامر الشراء"

    def invoice_model(self):
        return PurchaseInvoice

    def fulfillment_model(self):
        return PurchaseReceipt

    @property
    def available_actions(self):
        if self.is_open_for_billing:
            actions = [("make_invoice", "إنشاء فاتورة مشتريات"), ("close_order", "إغلاق الأمر")]
            if self.remaining_to_deliver():
                actions.insert(0, ("make_fulfillment", "إنشاء إذن استلام"))
            return actions
        if self.status == CommercialStatus.CLOSED:
            return [("reopen_order", "إعادة فتح الأمر")]
        return []

    @property
    def linked_documents(self):
        return list(self.receipts.all()) + list(self.invoices.all())

    def before_cancel(self):
        super().before_cancel()
        if self.receipts.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الأمر لوجود أذون استلام معتمدة عليه. ألغِها أولاً.")

    def validate_for_submit(self):
        super().validate_for_submit()
        if self.discount_amount or self.prices_include_tax:
            raise ValidationError("أوامر الشراء تُدخل بأسعار صافية غير شاملة للضريبة وبدون خصم إجمالي.")

    def on_submit(self):
        super().on_submit()
        self._update_sources(+1)

    def on_cancel(self):
        super().on_cancel()
        self._update_sources(-1)

    def _update_sources(self, sign):
        rows = list(self.items.select_related("material_request_item__parent", "supplier_quotation_item__parent"))
        update_request_ordered(rows, sign)
        for quote in {r.supplier_quotation_item.parent for r in rows if r.supplier_quotation_item_id}:
            quote.refresh_status()


class PurchaseOrderItem(OrderItem):
    parent = models.ForeignKey(PurchaseOrder, models.CASCADE, related_name="items")
    material_request_item = models.ForeignKey("buying.MaterialRequestItem", models.SET_NULL, null=True, blank=True,
                                              editable=False, related_name="order_items", verbose_name="بند طلب المواد")
    supplier_quotation_item = models.ForeignKey("buying.SupplierQuotationItem", models.SET_NULL, null=True,
                                                blank=True, editable=False, related_name="order_items",
                                                verbose_name="بند عرض سعر المورد")

    class Meta:
        verbose_name = "بند أمر شراء"
        verbose_name_plural = "الأصناف"


class MaterialRequestPurpose(models.TextChoices):
    PURCHASE = "purchase", "شراء"
    TRANSFER = "transfer", "تحويل من مستودع آخر"


class MaterialRequestStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    PENDING = "pending", "بانتظار التنفيذ"
    PARTIAL = "partial", "منفذ جزئياً"
    ORDERED = "ordered", "منفذ بالكامل"
    CLOSED = "closed", "مغلق"
    CANCELLED = "cancelled", "ملغى"


class MaterialRequest(SubmittableDocument):
    """طلب مواد داخلي من قسم أو مستودع: يتحول إلى طلب عروض أسعار من الموردين أو أمر شراء أو تحويل مخزني."""

    naming_prefix = "MR"
    posts_to_ledger = False

    purpose = models.CharField("الغرض", max_length=10, choices=MaterialRequestPurpose.choices,
                               default=MaterialRequestPurpose.PURCHASE)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+", verbose_name="للمستودع")
    from_warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+",
                                       verbose_name="من مستودع (للتحويل)")
    required_by = models.DateField("مطلوب قبل", null=True, blank=True)
    department = models.ForeignKey("hr.Department", models.SET_NULL, null=True, blank=True, related_name="+",
                                   verbose_name="القسم الطالب")
    status = models.CharField("الحالة", max_length=10, choices=MaterialRequestStatus.choices,
                              default=MaterialRequestStatus.DRAFT, editable=False)
    per_ordered = models.DecimalField("نسبة التنفيذ %", max_digits=6, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "طلب مواد"
        verbose_name_plural = "طلبات المواد (الشراء الداخلية)"

    posting_note = "طلب داخلي لا يُنشئ قيوداً؛ نفّذه بطلب عروض أسعار أو أمر شراء أو تحويل مخزني."

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        if not rows:
            raise ValidationError("أضف صنفاً واحداً على الأقل.")
        if any(r.qty <= 0 for r in rows):
            raise ValidationError("الكميات يجب أن تكون أكبر من صفر.")
        if self.purpose == MaterialRequestPurpose.TRANSFER:
            if not self.from_warehouse_id or self.from_warehouse_id == self.warehouse_id:
                raise ValidationError("حدد مستودع مصدر مختلفاً للتحويل.")

    def on_submit(self):
        self.status = MaterialRequestStatus.PENDING
        self.save(update_fields=["status"])

    def on_cancel(self):
        if any(r.ordered_qty for r in self.items.all()):
            raise ValidationError("نُفذ جزء من الطلب؛ ألغِ أوامر الشراء أو التحويلات المرتبطة أولاً.")
        self.status = MaterialRequestStatus.CANCELLED
        self.save(update_fields=["status"])

    def remaining(self):
        return {row: row.qty - row.ordered_qty for row in self.items.select_related("item") if row.qty > row.ordered_qty}

    def refresh_status(self):
        rows = list(self.items.all())
        requested = sum((r.qty for r in rows), flt(0, 3))
        done = sum((min(r.ordered_qty, r.qty) for r in rows), flt(0, 3))
        self.per_ordered = flt(done * 100 / requested) if requested else flt(0)
        if self.status not in (MaterialRequestStatus.CLOSED, MaterialRequestStatus.CANCELLED):
            self.status = (MaterialRequestStatus.ORDERED if done >= requested else
                           MaterialRequestStatus.PARTIAL if done > 0 else MaterialRequestStatus.PENDING)
        self.save(update_fields=["per_ordered", "status"])

    @property
    def is_open(self):
        return self.is_submitted and self.status in (MaterialRequestStatus.PENDING, MaterialRequestStatus.PARTIAL)

    @property
    def available_actions(self):
        if not self.is_open:
            return []
        if self.purpose == MaterialRequestPurpose.TRANSFER:
            return [("make_transfer", "إنشاء تحويل مخزني"), ("close_request", "إغلاق الطلب")]
        return [("make_supplier_quotation", "طلب عرض سعر من مورد"), ("make_purchase_order", "إنشاء أمر شراء"),
                ("close_request", "إغلاق الطلب")]

    @property
    def linked_documents(self):
        if not self.pk:
            return []
        quotes = SupplierQuotation.objects.filter(items__material_request_item__parent=self).distinct()
        orders = PurchaseOrder.objects.filter(items__material_request_item__parent=self).distinct()
        return list(quotes) + list(orders)

    def quotation_comparison(self):
        """مقارنة عروض الموردين المعتمدة لكل صنف: [{"item", "qty", "offers": [{supplier, rate, quote, best}]}]."""
        rows = []
        for mr_item in self.items.select_related("item"):
            offers = []
            for q in (SupplierQuotationItem.objects.filter(material_request_item=mr_item, parent__docstatus=1)
                      .select_related("parent", "parent__supplier")):
                base = flt(q.stock_rate * (q.parent.exchange_rate or 1))  # بعملة الشركة لكل وحدة أساسية
                offers.append({"supplier": q.parent.supplier, "rate": q.rate, "base_rate": base, "quote": q.parent,
                               "lead_days": q.parent.lead_days, "best": False})
            if offers:
                min(offers, key=lambda o: o["base_rate"])["best"] = True
            rows.append({"item": mr_item.item, "qty": mr_item.qty, "offers": sorted(offers, key=lambda o: o["base_rate"])})
        return rows

    def _check_open(self):
        if not self.is_open:
            raise ValidationError("الطلب غير معتمد أو منفذ بالكامل أو مغلق.")

    def _lines(self, target, link=True):
        from django.db import transaction

        remaining = self.remaining()
        if not remaining:
            raise ValidationError("لا توجد كميات متبقية في الطلب.")
        with transaction.atomic():
            target.save()
            for row, qty in remaining.items():
                line = {"item": row.item, "qty": qty, "uom_id": row.uom_id,
                        "rate": row.item.price_for(row.uom_id, "buying") if hasattr(target, "supplier_id") else 0}
                if link:
                    line["material_request_item"] = row
                target.items.create(**line)
            if hasattr(target, "calculate_totals"):
                target.calculate_totals()
                target.save()
        return target

    def make_supplier_quotation(self, user=None):
        self._check_open()
        return self._lines(SupplierQuotation(company=self.company, created_by=user, warehouse=self.warehouse,
                                             remarks=f"طلب عرض سعر للطلب {self.name}"))

    def make_purchase_order(self, user=None):
        self._check_open()
        supplier = Supplier.objects.filter(disabled=False).first()
        if supplier is None:
            raise ValidationError("أضف مورداً أولاً.")
        return self._lines(PurchaseOrder(company=self.company, created_by=user, supplier=supplier,
                                         warehouse=self.warehouse, schedule_date=self.required_by,
                                         remarks=f"أمر شراء للطلب {self.name} — راجع المورد قبل الاعتماد"))

    def make_transfer(self, user=None):
        from django.db import transaction

        from stock.models import StockEntry, StockEntryPurpose

        self._check_open()
        with transaction.atomic():
            entry = StockEntry.objects.create(company=self.company, created_by=user, purpose=StockEntryPurpose.TRANSFER,
                                              from_warehouse=self.from_warehouse, to_warehouse=self.warehouse,
                                              material_request=self, remarks=f"تحويل للطلب {self.name}")
            for row, qty in self.remaining().items():
                entry.items.create(item=row.item, qty=flt(qty * row.conversion_factor, 3))
            entry.calculate_totals()
            entry.save()
        return entry

    def close_request(self, user=None):
        self._check_open()
        self.status = MaterialRequestStatus.CLOSED
        self.save(update_fields=["status"])


class MaterialRequestItem(models.Model):
    parent = models.ForeignKey(MaterialRequest, models.CASCADE, related_name="items")
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="الصنف")
    uom = models.ForeignKey("stock.UOM", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="الوحدة",
                            help_text="فارغ = الوحدة الأساسية")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    conversion_factor = models.DecimalField("معامل التحويل", max_digits=18, decimal_places=6, default=1, editable=False)
    ordered_qty = models.DecimalField("المنفذ", max_digits=18, decimal_places=3, default=0, editable=False)
    notes = models.CharField("ملاحظات", max_length=150, blank=True)

    class Meta:
        verbose_name = "بند طلب مواد"
        verbose_name_plural = "الأصناف المطلوبة"

    def save(self, *args, **kwargs):
        if self.item_id:
            self.conversion_factor = self.item.uom_factor(self.uom_id)
        super().save(*args, **kwargs)


def update_request_ordered(rows, sign):
    """تحديث المنفذ من طلبات المواد عند اعتماد (+1) أو إلغاء (-1) أمر شراء أو تحويل مرتبط."""
    requests = set()
    for row in rows:
        mr_item = row.material_request_item
        if mr_item is None:
            continue
        qty = flt(flt(row.qty, 3) * flt(getattr(row, "conversion_factor", 1) or 1, 6) / flt(mr_item.conversion_factor or 1, 6), 3)
        mr_item.ordered_qty = max(flt(mr_item.ordered_qty + sign * qty, 3), flt(0, 3))
        mr_item.save(update_fields=["ordered_qty"])
        requests.add(mr_item.parent)
    for request in requests:
        request.refresh_status()


class SupplierQuotation(CommercialDocument):
    """عرض سعر مورد: يُطلب من عدة موردين لنفس طلب المواد، ويُقارن، ويُحوّل الأفضل إلى أمر شراء."""

    naming_prefix = "SQ"
    party_field = "supplier"

    supplier = models.ForeignKey(Supplier, models.PROTECT, null=True, blank=True, limit_choices_to={"disabled": False},
                                 verbose_name="المورد")
    valid_till = models.DateField("صالح حتى", null=True, blank=True)
    lead_days = models.PositiveIntegerField("مدة التوريد (يوم)", default=0)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+",
                                  verbose_name="مستودع الاستلام")

    class Meta(CommercialDocument.Meta):
        verbose_name = "عرض سعر مورد"
        verbose_name_plural = "عروض أسعار الموردين"

    def validate_for_submit(self):
        if not self.supplier_id:
            raise ValidationError("حدد المورد.")
        super().validate_for_submit()

    @property
    def available_actions(self):
        if self.is_submitted and self.status == CommercialStatus.OPEN:
            actions = [("make_purchase_order", "إنشاء أمر شراء")]
            if self.items.filter(material_request_item__isnull=False).exists():
                actions.append(("copy_for_supplier", "نسخة لمورد آخر"))
            return actions
        return []

    @property
    def linked_documents(self):
        return list(PurchaseOrder.objects.filter(items__supplier_quotation_item__parent=self).distinct()) if self.pk else []

    def make_purchase_order(self, user=None):
        if not (self.is_submitted and self.status == CommercialStatus.OPEN):
            raise ValidationError("يمكن تحويل العروض المعتمدة المفتوحة فقط.")
        order = self._copy_to(PurchaseOrder, user, {row: row.qty for row in self.items.all()},
                              link_field="supplier_quotation_item",
                              extra={"warehouse": self.warehouse,
                                     "schedule_date": self.posting_date + __import__("datetime").timedelta(days=self.lead_days)})
        for row in order.items.select_related("supplier_quotation_item"):
            row.material_request_item_id = row.supplier_quotation_item.material_request_item_id
            row.save(update_fields=["material_request_item"])
        return order

    def copy_for_supplier(self, user=None):
        copy = SupplierQuotation.objects.create(company=self.company, created_by=user, warehouse=self.warehouse,
                                                remarks=self.remarks)
        for row in self.items.all():
            copy.items.create(item=row.item, qty=row.qty, uom_id=row.uom_id, rate=0,
                              material_request_item_id=row.material_request_item_id)
        copy.calculate_totals()
        copy.save()
        return copy

    def refresh_status(self):
        if self.is_submitted:
            ordered = PurchaseOrder.objects.filter(items__supplier_quotation_item__parent=self, docstatus=1).exists()
            self.status = CommercialStatus.ORDERED if ordered else CommercialStatus.OPEN
            self.save(update_fields=["status"])


class SupplierQuotationItem(CommercialItem):
    parent = models.ForeignKey(SupplierQuotation, models.CASCADE, related_name="items")
    material_request_item = models.ForeignKey(MaterialRequestItem, models.SET_NULL, null=True, blank=True,
                                              editable=False, related_name="quotation_items",
                                              verbose_name="بند طلب المواد")

    class Meta:
        verbose_name = "بند عرض سعر مورد"
        verbose_name_plural = "الأصناف"


class PurchaseInvoice(InvoiceDocument):
    naming_prefix = "PINV"
    allow_expired_batches = True  # مرتجع المشتريات يُعيد البضاعة المنتهية للمورد
    return_prefix = "PRET"
    party_field = "supplier"
    return_locked_fields = InvoiceDocument.return_locked_fields + ["supplier"]

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    bill_no = models.CharField("رقم فاتورة المورد", max_length=50, blank=True)
    order = models.ForeignKey(PurchaseOrder, models.PROTECT, null=True, blank=True, related_name="invoices",
                              editable=False, verbose_name="أمر الشراء")

    class Meta(InvoiceDocument.Meta):
        verbose_name = "فاتورة مشتريات"
        verbose_name_plural = "فواتير المشتريات"

    @property
    def document_title(self):
        return "إشعار مدين (مرتجع مشتريات)" if self.is_return else "فاتورة مشتريات"

    def validate_for_submit(self):
        super().validate_for_submit()
        if self.discount_amount:
            raise ValidationError("الخصم في فاتورة المشتريات غير مدعوم حالياً؛ أدخل الأسعار بعد الخصم.")
        if self.update_stock and not self.is_return:
            rows = list(self.items.select_related("order_item", "receipt_item"))
            if any(r.receipt_item_id for r in rows):
                raise ValidationError("البضاعة استُلمت بإذن الاستلام؛ ألغِ «تحديث المخزون» في هذه الفاتورة.")
            if any(r.order_item_id and r.order_item.delivered_qty > 0 for r in rows):
                raise ValidationError("صدرت أذون استلام لهذا الأمر؛ أنشئ الفاتورة من إذن الاستلام "
                                      "أو ألغِ «تحديث المخزون» حتى لا تُدخل البضاعة مرتين.")

    @property
    def linked_documents(self):
        return [self.order] if self.order_id else []

    def on_submit(self):
        update_order_billing(self, +1)
        update_fulfillment_billing(self, "receipt_item", +1)
        company = self.company
        entries = []
        for row in self.items.select_related("item", "item__expense_account", "receipt_item__parent"):
            if row.receipt_item_id and not self.is_return:
                # البضاعة دخلت المخزون بإذن الاستلام: يُقفل الحساب الوسيط بقيمة الاستلام، وفرق السعر لتسويات المخزون
                received = flt(linked_qty(row, row.receipt_item) * row.receipt_item.base_rate)
                base_amount = self.to_base(row.amount)
                entries.append({"account": srbnb_account(company), "debit": received, "base": True})
                if base_amount != received:
                    entries.append({"account": company.require_account("default_stock_adjustment_account"),
                                    "debit": base_amount - received, "base": True})
            elif self.update_stock and row.item.is_stock_item:
                # المخزون يُقيّم بعملة الشركة: سعر الشراء × سعر الصرف.
                # المرتجع يخرج بسعر التقييم الحالي؛ فرقه عن سعر الشراء يذهب لحساب تسويات المخزون
                base_amount = self.to_base(row.amount)
                value = make_stock_ledger_entry(self, row.item, self.warehouse, row.stock_qty,
                                                row.stock_rate * self.exchange_rate,
                                                batch_no=row.batch_no, expiry_date=row.expiry_date)
                entries.append({"account": company.require_account("default_stock_account"), "debit": value, "base": True})
                if base_amount != value:
                    entries.append({
                        "account": company.require_account("default_stock_adjustment_account"),
                        "debit": base_amount - value, "base": True,
                    })
            else:
                account = row.item.expense_account or company.require_account("default_expense_account")
                entries.append({"account": account, "debit": row.amount})
        if self.tax_amount:
            entries.append({"account": company.require_account("default_input_tax_account"), "debit": self.tax_amount})
        entries.append({
            "account": company.require_account("default_payable_account"),
            "credit": self.grand_total, "supplier": self.supplier,
        })
        default_remarks = (f"مرتجع مشتريات من الفاتورة {self.return_against}" if self.is_return
                           else f"فاتورة مشتريات من المورد {self.supplier}")
        make_gl_entries(self, entries, remarks=self.remarks or default_remarks, exchange_rate=self.exchange_rate)
        self.apply_return_to_original()
        update_buying_rates(self)

    def before_cancel(self):
        super().before_cancel()
        if self.landed_costs.filter(docstatus=1).exists():
            raise ValidationError("حُمّلت على هذه الفاتورة تكاليف استيراد معتمدة. ألغِ مستند التحميل أولاً.")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        self.unapply_return_from_original()
        update_order_billing(self, -1)
        update_fulfillment_billing(self, "receipt_item", -1)
        update_buying_rates(self, cancelled=True)


class PurchaseInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name="items")
    order_item = models.ForeignKey(PurchaseOrderItem, models.PROTECT, null=True, blank=True, editable=False,
                                   related_name="invoice_items", verbose_name="بند أمر الشراء")
    receipt_item = models.ForeignKey("buying.PurchaseReceiptItem", models.PROTECT, null=True, blank=True,
                                     editable=False, related_name="invoice_items", verbose_name="بند إذن الاستلام")

    batch_no = models.CharField("رقم التشغيلة", max_length=40, blank=True)
    expiry_date = models.DateField("تاريخ الانتهاء", null=True, blank=True)

    class Meta:
        verbose_name = "بند فاتورة مشتريات"
        verbose_name_plural = "الأصناف"


class PurchaseReceipt(FulfillmentDocument):
    """إذن استلام: يُدخل البضاعة للمخزون قبل وصول فاتورة المورد، مقابل حساب وسيط."""

    naming_prefix = "PR"
    party_field = "supplier"
    invoice_link_field = "receipt_item"

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    supplier_delivery_no = models.CharField("رقم إشعار تسليم المورد", max_length=50, blank=True)
    order = models.ForeignKey(PurchaseOrder, models.PROTECT, null=True, blank=True, related_name="receipts",
                              editable=False, verbose_name="أمر الشراء")

    class Meta(FulfillmentDocument.Meta):
        verbose_name = "إذن استلام"
        verbose_name_plural = "أذون الاستلام"

    def invoice_model(self):
        return PurchaseInvoice

    @property
    def linked_documents(self):
        invoices = PurchaseInvoice.objects.filter(items__receipt_item__parent=self).distinct()
        return ([self.order] if self.order_id else []) + list(invoices)

    def on_submit(self):
        value = flt(0)
        for row in self.items.select_related("item"):
            value += make_stock_ledger_entry(self, row.item, self.warehouse, row.stock_qty,
                                             row.stock_rate * self.exchange_rate,
                                             batch_no=row.batch_no, expiry_date=row.expiry_date)
        if value:
            make_gl_entries(self, [
                {"account": self.company.require_account("default_stock_account"), "debit": value, "base": True},
                {"account": srbnb_account(self.company), "credit": value, "base": True},
            ], remarks=self.remarks or f"استلام بضاعة من المورد {self.supplier}")
        self.update_order_delivery(+1)
        update_buying_rates(self)

    def before_cancel(self):
        super().before_cancel()
        if self.landed_costs.filter(docstatus=1).exists():
            raise ValidationError("حُمّلت على هذا الإذن تكاليف استيراد معتمدة. ألغِ مستند التحميل أولاً.")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        self.update_order_delivery(-1)
        update_buying_rates(self, cancelled=True)


class PurchaseReceiptItem(FulfillmentItem):
    parent = models.ForeignKey(PurchaseReceipt, models.CASCADE, related_name="items")
    order_item = models.ForeignKey(PurchaseOrderItem, models.PROTECT, null=True, blank=True, editable=False,
                                   related_name="receipt_items", verbose_name="بند أمر الشراء")

    batch_no = models.CharField("رقم التشغيلة", max_length=40, blank=True)
    expiry_date = models.DateField("تاريخ الانتهاء", null=True, blank=True)

    class Meta:
        verbose_name = "بند إذن استلام"
        verbose_name_plural = "الأصناف"

    @property
    def base_rate(self):
        return self.rate * (self.parent.exchange_rate or 1)


class AllocationBasis(models.TextChoices):
    AMOUNT = "amount", "حسب قيمة الأصناف"
    QTY = "qty", "حسب الكمية"


class LandedCostVoucher(SubmittableDocument):
    """تحميل التكاليف الإضافية (شحن، جمارك، تخليص، تأمين) على تكلفة بضاعة مستلمة.

    تُوزع التكاليف على أصناف المستند بالقيمة أو بالكمية؛ ما بقي منها في المخزون تُرفع به قيمة الرصيد
    (فيرتفع متوسط التكلفة)، وما بيع قبل التحميل يذهب لتكلفة البضاعة المباعة.
    """

    naming_prefix = "LCV"

    purchase_receipt = models.ForeignKey(PurchaseReceipt, models.PROTECT, null=True, blank=True,
                                         related_name="landed_costs", verbose_name="إذن الاستلام",
                                         limit_choices_to={"docstatus": 1})
    purchase_invoice = models.ForeignKey(PurchaseInvoice, models.PROTECT, null=True, blank=True,
                                         related_name="landed_costs", verbose_name="فاتورة المشتريات (محدّثة للمخزون)",
                                         limit_choices_to={"docstatus": 1, "update_stock": True, "is_return": False})
    basis = models.CharField("أساس التوزيع", max_length=10, choices=AllocationBasis.choices,
                             default=AllocationBasis.AMOUNT)
    total_charges = models.DecimalField("إجمالي التكاليف", max_digits=18, decimal_places=2, default=0, editable=False)
    capitalized = models.DecimalField("المحمّل على المخزون", max_digits=18, decimal_places=2, default=0, editable=False)
    expensed = models.DecimalField("المحمّل على تكلفة المبيعات", max_digits=18, decimal_places=2, default=0,
                                   editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "تحميل تكاليف الاستيراد"
        verbose_name_plural = "تحميل تكاليف الشحن والجمارك"

    @property
    def source(self):
        return self.purchase_receipt or self.purchase_invoice

    @property
    def linked_documents(self):
        return [self.source] if self.source else []

    def calculate_totals(self):
        self.total_charges = sum((flt(c.amount) for c in self.charges.all()), flt(0))

    def stock_rows(self):
        """{(الصنف، المستودع): [الكمية بالوحدة الأساسية، القيمة بعملة الشركة]} للأصناف المخزنية في المستند."""
        source = self.source
        rows = {}
        for row in source.items.select_related("item"):
            if not row.item.is_stock_item:
                continue
            entry = rows.setdefault((row.item, source.warehouse), [flt(0, 3), flt(0)])
            entry[0] += row.stock_qty
            entry[1] += flt(row.amount * (source.exchange_rate or 1))
        return rows

    def validate_for_submit(self):
        if bool(self.purchase_receipt_id) == bool(self.purchase_invoice_id):
            raise ValidationError("اختر إذن استلام أو فاتورة مشتريات واحدة.")
        source = self.source
        if not source.is_submitted:
            raise ValidationError(f"المستند {source} غير معتمد.")
        if self.purchase_invoice_id and (not source.update_stock or source.is_return):
            raise ValidationError("فاتورة المشتريات يجب أن تكون محدّثة للمخزون وليست مرتجعاً.")
        if not self.stock_rows():
            raise ValidationError("لا توجد أصناف مخزنية في المستند لتحميل التكاليف عليها.")
        charges = list(self.charges.select_related("account"))
        if not charges or self.total_charges <= 0:
            raise ValidationError("أضف التكاليف (الشحن، الجمارك...) بمبالغ موجبة.")
        for charge in charges:
            if flt(charge.amount) <= 0:
                raise ValidationError(f"مبلغ «{charge.description or charge.account}» يجب أن يكون أكبر من صفر.")
            if charge.account.is_group:
                raise ValidationError(f"لا يُرحّل على الحساب الرئيسي {charge.account}.")

    def allocations(self):
        """توزيع إجمالي التكاليف على الأصناف: {(الصنف، المستودع): المبلغ}."""
        from core.utils import allocate

        rows = self.stock_rows()
        weights = {key: (qty if self.basis == AllocationBasis.QTY else value) for key, (qty, value) in rows.items()}
        if not any(weights.values()):
            weights = {key: qty for key, (qty, _value) in rows.items()}
        return allocate(self.total_charges, weights), rows

    def on_submit(self):
        company = self.company
        shares, rows = self.allocations()
        to_stock = to_cogs = flt(0)
        for (item, warehouse), share in shares.items():
            received = rows[(item, warehouse)][0]
            from stock.models import Bin

            on_hand = flt(Bin.objects.filter(item=item, warehouse=warehouse).values_list("actual_qty", flat=True)
                          .first() or 0, 3)
            # ما زال في المخزون يُرفع به متوسط التكلفة، وما خرج قبل التحميل يُحمّل على تكلفة المبيعات
            in_stock = flt(share * min(max(on_hand, 0), received) / received) if received else flt(0)
            added = make_value_adjustment(self, item, warehouse, in_stock)
            to_stock += added
            to_cogs += share - added
        entries = [{"account": c.account, "credit": c.amount, "remarks": c.description or "تكاليف استيراد"}
                   for c in self.charges.select_related("account")]
        if to_stock:
            entries.append({"account": company.require_account("default_stock_account"), "debit": to_stock})
        if to_cogs:
            entries.append({"account": company.require_account("default_cogs_account"), "debit": to_cogs})
        make_gl_entries(self, entries, remarks=self.remarks or f"تحميل تكاليف على {self.source}")
        self.capitalized, self.expensed = to_stock, to_cogs
        self.save(update_fields=["capitalized", "expensed"])

    def before_cancel(self):
        from stock.models import Bin, StockLedgerEntry

        for sle in StockLedgerEntry.objects.filter(voucher_type=self.doctype_slug, voucher_id=self.pk,
                                                   is_cancelled=False).select_related("item", "warehouse"):
            qty = Bin.objects.filter(item=sle.item, warehouse=sle.warehouse).values_list("actual_qty", flat=True).first()
            if flt(qty or 0, 3) < flt(sle.qty_after_transaction, 3):
                raise ValidationError(f"صُرف جزء من «{sle.item.name}» بعد التحميل؛ لا يمكن الإلغاء. "
                                      "سجّل قيد تسوية بدلاً من ذلك.")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)


class LandedCostCharge(models.Model):
    parent = models.ForeignKey(LandedCostVoucher, models.CASCADE, related_name="charges")
    description = models.CharField("البيان", max_length=150, blank=True, help_text="مثل: شحن بحري، رسوم جمركية، تخليص")
    account = models.ForeignKey("accounts.Account", models.PROTECT, related_name="+", verbose_name="الحساب الدائن",
                                limit_choices_to={"is_group": False, "disabled": False},
                                help_text="الحساب الذي سُجلت عليه التكلفة (مصروف مؤقت، مورد الشحن، الصندوق…)")
    amount = models.DecimalField("المبلغ (بعملة الشركة)", max_digits=18, decimal_places=2, default=0)

    class Meta:
        verbose_name = "تكلفة إضافية"
        verbose_name_plural = "التكاليف"


class AIModel(models.TextChoices):
    OPUS = "claude-opus-5-5", "Claude Opus 5.5 — الأدق (موصى به)"
    SONNET = "claude-sonnet-5-5", "Claude Sonnet 5.5 — متوازن وأقل تكلفة"
    HAIKU = "claude-haiku-5-5", "Claude Haiku 5.5 — الأسرع والأرخص"


class AIInvoiceSettings(TenantModel):
    """ربط الشركة بحسابها لدى Anthropic (Claude) لقراءة فواتير الموردين. التكلفة على مفتاح الشركة نفسها."""

    enabled = models.BooleanField("تفعيل قراءة الفواتير بالذكاء الاصطناعي", default=False)
    api_key_encrypted = models.TextField(blank=True, editable=False)
    model = models.CharField("النموذج", max_length=40, choices=AIModel.choices, default=AIModel.OPUS)
    consent = models.BooleanField(
        "أوافق على إرسال صور وملفات فواتير الموردين إلى Anthropic لقراءتها", default=False,
        help_text="تُرسل الفاتورة المرفوعة فقط، وقت الرفع، ولا يُحفظ الملف في النظام.")
    invoices_read = models.PositiveIntegerField("عدد الفواتير المقروءة", default=0, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "إعدادات الذكاء الاصطناعي"
        verbose_name_plural = "إعدادات الذكاء الاصطناعي"
        constraints = [models.UniqueConstraint(fields=["company"], name="one_ai_settings_per_company")]

    @classmethod
    def for_company(cls, company):
        obj = cls.all_objects.filter(company=company).first()
        return obj or cls.all_objects.create(company=company)

    @property
    def has_key(self):
        return bool(self.api_key_encrypted)

    @property
    def api_key(self):
        from core.mail import decrypt_password

        return decrypt_password(self.api_key_encrypted)

    def set_api_key(self, key):
        from core.mail import encrypt_password

        self.api_key_encrypted = encrypt_password(key.strip()) if key else ""

    @property
    def active(self):
        return self.enabled and self.consent and self.has_key
