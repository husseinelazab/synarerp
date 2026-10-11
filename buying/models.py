from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.commercial import CommercialStatus, OrderDocument, OrderItem, update_order_billing
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


class PurchaseOrderItem(OrderItem):
    parent = models.ForeignKey(PurchaseOrder, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند أمر شراء"
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
