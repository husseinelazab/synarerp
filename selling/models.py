from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.commercial import (CommercialDocument, CommercialItem, CommercialStatus, OrderDocument, OrderItem,
                             update_order_billing)
from core.fulfillment import FulfillmentDocument, FulfillmentItem, update_fulfillment_billing
from core.invoice import InvoiceDocument, InvoiceItem
from core.models import TenantModel
from core.tenancy import tenant_unique
from core.utils import allocate, flt
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry


class CustomerType(models.TextChoices):
    COMPANY = "company", "شركة / منشأة"
    INDIVIDUAL = "individual", "فرد"


class Customer(TenantModel):
    name = models.CharField("اسم العميل", max_length=200)
    customer_type = models.CharField("نوع العميل", max_length=20, choices=CustomerType.choices, default=CustomerType.COMPANY)
    tax_id = models.CharField("الرقم الضريبي", max_length=20, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=100, blank=True)
    address = models.TextField("العنوان", blank=True)
    street = models.CharField("الشارع", max_length=100, blank=True)
    building_number = models.CharField("رقم المبنى", max_length=4, blank=True)
    district = models.CharField("الحي", max_length=100, blank=True)
    postal_code = models.CharField("الرمز البريدي", max_length=5, blank=True)
    country_code = models.CharField("رمز الدولة", max_length=2, default="SA")
    credit_limit = models.DecimalField("حد الائتمان", max_digits=18, decimal_places=2, default=0,
                                       help_text="صفر = بلا حد. يُمنع البيع الآجل الذي يتجاوز الحد")
    credit_hold = models.BooleanField("إيقاف البيع الآجل", default=False,
                                      help_text="يُسمح بالبيع النقدي فقط لهذا العميل")
    price_list = models.ForeignKey("stock.PriceList", models.SET_NULL, null=True, blank=True, related_name="+",
                                   limit_choices_to={"disabled": False}, verbose_name="قائمة الأسعار",
                                   help_text="أسعار هذه القائمة تُقترح في عروض الأسعار والأوامر والفواتير للعميل")
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "عميل"
        verbose_name_plural = "العملاء"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="customer")]

    def __str__(self):
        return self.name

    @property
    def credit_status(self):
        from accounts.credit import credit_status

        return credit_status(self)


class Quotation(CommercialDocument):
    naming_prefix = "QTN"

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    valid_till = models.DateField("صالح حتى", null=True, blank=True)

    class Meta(CommercialDocument.Meta):
        verbose_name = "عرض سعر"
        verbose_name_plural = "عروض الأسعار"

    @property
    def is_expired(self):
        from django.utils import timezone

        return self.status == CommercialStatus.OPEN and self.valid_till and self.valid_till < timezone.localdate()

    @property
    def available_actions(self):
        if self.is_submitted and self.status == CommercialStatus.OPEN:
            return [("make_sales_order", "إنشاء أمر بيع"), ("make_invoice", "إنشاء فاتورة مبيعات")]
        return []

    @property
    def linked_documents(self):
        return list(self.sales_orders.all()) + list(self.invoices.all())

    def _check_convertible(self):
        if not (self.is_submitted and self.status == CommercialStatus.OPEN):
            raise ValidationError("يمكن تحويل عروض الأسعار المعتمدة المفتوحة فقط.")

    def make_sales_order(self, user=None):
        self._check_convertible()
        return self._copy_to(SalesOrder, user, {row: row.qty for row in self.items.all()}, extra={"quotation": self})

    def make_invoice(self, user=None):
        self._check_convertible()
        return self._copy_to(SalesInvoice, user, {row: row.qty for row in self.items.all()}, extra={"quotation": self})

    def refresh_status(self):
        """العرض «تم التحويل» ما دام له أمر بيع أو فاتورة معتمدة."""
        if not self.is_submitted:
            return
        converted = self.sales_orders.filter(docstatus=1).exists() or self.invoices.filter(docstatus=1).exists()
        self.status = CommercialStatus.ORDERED if converted else CommercialStatus.OPEN
        self.save(update_fields=["status"])


class QuotationItem(CommercialItem):
    parent = models.ForeignKey(Quotation, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند عرض سعر"
        verbose_name_plural = "الأصناف"
        ordering = ["id"]


class SalesOrder(OrderDocument):
    naming_prefix = "SO"

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    delivery_date = models.DateField("تاريخ التسليم المتوقع", null=True, blank=True)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+",
                                  verbose_name="المستودع")
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False},
                                    verbose_name="مركز التكلفة / الفرع")
    quotation = models.ForeignKey(Quotation, models.PROTECT, null=True, blank=True, related_name="sales_orders",
                                  editable=False, verbose_name="عرض السعر")

    class Meta(OrderDocument.Meta):
        verbose_name = "أمر بيع"
        verbose_name_plural = "أوامر البيع"

    def validate_for_submit(self):
        super().validate_for_submit()
        from accounts.credit import check_credit

        check_credit(self, self.base_grand_total)

    def invoice_model(self):
        return SalesInvoice

    def fulfillment_model(self):
        return DeliveryNote

    @property
    def available_actions(self):
        if self.is_open_for_billing:
            actions = [("make_invoice", "إنشاء فاتورة مبيعات"), ("close_order", "إغلاق الأمر")]
            if self.remaining_to_deliver():
                actions.insert(0, ("make_fulfillment", "إنشاء إذن تسليم"))
            return actions
        if self.status == CommercialStatus.CLOSED:
            return [("reopen_order", "إعادة فتح الأمر")]
        return []

    @property
    def linked_documents(self):
        return ([self.quotation] if self.quotation_id else []) + list(self.delivery_notes.all()) + list(self.invoices.all())

    def before_cancel(self):
        super().before_cancel()
        if self.delivery_notes.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الأمر لوجود أذون تسليم معتمدة عليه. ألغِها أولاً.")

    def on_submit(self):
        super().on_submit()
        if self.quotation_id:
            self.quotation.refresh_status()

    def on_cancel(self):
        super().on_cancel()
        if self.quotation_id:
            self.quotation.refresh_status()


class SalesOrderItem(OrderItem):
    parent = models.ForeignKey(SalesOrder, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند أمر بيع"
        verbose_name_plural = "الأصناف"
        ordering = ["id"]


class SalesInvoice(InvoiceDocument):
    naming_prefix = "SINV"
    return_prefix = "SRET"
    party_field = "customer"
    return_locked_fields = InvoiceDocument.return_locked_fields + ["prices_include_tax", "customer"]

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    prices_include_tax = models.BooleanField("الأسعار شاملة الضريبة", default=False)
    order = models.ForeignKey(SalesOrder, models.PROTECT, null=True, blank=True, related_name="invoices",
                              editable=False, verbose_name="أمر البيع")
    quotation = models.ForeignKey(Quotation, models.PROTECT, null=True, blank=True, related_name="invoices",
                                  editable=False, verbose_name="عرض السعر")
    share_token = models.CharField("رمز رابط المشاركة", max_length=40, blank=True, db_index=True, editable=False)
    recurring = models.ForeignKey("RecurringInvoice", models.SET_NULL, null=True, blank=True, related_name="+",
                                  editable=False, verbose_name="الفاتورة المتكررة")

    # حقول نقاط البيع
    is_pos = models.BooleanField("فاتورة نقطة بيع", default=False, editable=False)
    pos_shift = models.ForeignKey(
        "pos.POSShift", models.PROTECT, null=True, blank=True, related_name="invoices", editable=False, verbose_name="الوردية"
    )
    paid_cash = models.DecimalField("المدفوع نقداً", max_digits=18, decimal_places=2, default=0, editable=False)
    paid_card = models.DecimalField("المدفوع بالبطاقة", max_digits=18, decimal_places=2, default=0, editable=False)
    change_amount = models.DecimalField("المبلغ المرتجع (الباقي)", max_digits=18, decimal_places=2, default=0, editable=False)
    # معرّف البيع لدى جهاز نقطة البيع عند البيع دون اتصال؛ يمنع تكرار الفاتورة عند إعادة المزامنة
    offline_id = models.CharField("معرّف البيع دون اتصال", max_length=64, blank=True, editable=False, db_index=True)
    offline_at = models.DateTimeField("وقت البيع دون اتصال", null=True, blank=True, editable=False)

    # الإشعار المدين: زيادة على فاتورة سابقة (فرق سعر، رسوم إضافية...) دون حركة مخزون
    is_debit_note = models.BooleanField("إشعار مدين", default=False, editable=False)
    debit_note_against = models.ForeignKey("self", models.PROTECT, null=True, blank=True, related_name="debit_notes",
                                           editable=False, verbose_name="إشعار مدين على الفاتورة")

    class Meta(InvoiceDocument.Meta):
        verbose_name = "فاتورة مبيعات"
        verbose_name_plural = "فواتير المبيعات"

    def share_path(self):
        """رابط عام للفاتورة يفتحه العميل دون تسجيل دخول (يُنشأ الرمز عند أول مشاركة)."""
        import secrets

        from django.urls import reverse

        if not self.share_token:
            self.share_token = secrets.token_urlsafe(24)
            type(self).all_objects.filter(pk=self.pk).update(share_token=self.share_token)
        return reverse("public_invoice", args=[self.share_token])

    def share_url(self):
        from django.conf import settings

        return settings.SYNAR_SITE_URL.rstrip("/") + self.share_path()

    @property
    def cash_received(self):
        return flt(self.paid_cash - self.change_amount)

    def get_naming_prefix(self):
        return "SDN" if self.is_debit_note else super().get_naming_prefix()

    @property
    def available_actions(self):
        if self.is_submitted and not self.is_return and not self.is_debit_note:
            return [("make_debit_note", "إنشاء إشعار مدين")]
        return []

    def make_debit_note(self, user=None):
        """مسودة إشعار مدين على هذه الفاتورة بنفس العميل والعملة والضريبة؛ تُضاف بنود الزيادة يدوياً."""
        if not self.is_submitted or self.is_return or self.is_debit_note:
            raise ValidationError("يُنشأ الإشعار المدين من فاتورة مبيعات معتمدة فقط.")
        note = SalesInvoice.objects.create(
            company=self.company, customer=self.customer, is_debit_note=True, debit_note_against=self,
            currency=self.currency, exchange_rate=self.exchange_rate, tax_rate=self.tax_rate,
            vat_category=self.vat_category, vat_exemption_reason=self.vat_exemption_reason,
            prices_include_tax=self.prices_include_tax, cost_center=self.cost_center, update_stock=False,
            created_by=user, remarks=f"إشعار مدين على الفاتورة {self.name}: ")
        return note

    @property
    def document_title(self):
        if self.is_debit_note:
            return "إشعار مدين مبسط" if self.is_pos else "إشعار مدين ضريبي"
        if self.is_return:
            return "إشعار دائن مبسط" if self.is_pos else "إشعار دائن (مرتجع مبيعات)"
        return "فاتورة ضريبية مبسطة" if self.is_pos else "فاتورة ضريبية"

    def validate_for_submit(self):
        super().validate_for_submit()
        if not self.is_return:
            from accounts.credit import check_credit

            # الجزء الآجل فقط: ما دُفع في نقطة البيع لا يُحسب على حد الائتمان
            paid = flt(self.paid_cash + self.paid_card - self.change_amount) if self.is_pos else flt(0)
            check_credit(self, self.to_base(max(self.grand_total - paid, flt(0))))
        if self.update_stock and not self.is_return:
            rows = list(self.items.select_related("order_item", "delivery_note_item"))
            if any(r.delivery_note_item_id for r in rows):
                raise ValidationError("البضاعة سُلّمت بإذن التسليم؛ ألغِ «تحديث المخزون» في هذه الفاتورة.")
            if any(r.order_item_id and r.order_item.delivered_qty > 0 for r in rows):
                raise ValidationError("صدرت أذون تسليم لهذا الأمر؛ أنشئ الفاتورة من إذن التسليم "
                                      "أو ألغِ «تحديث المخزون» حتى لا تُخرج البضاعة مرتين.")
        if self.is_debit_note:
            original = self.debit_note_against
            if original is None or not original.is_submitted:
                raise ValidationError("الإشعار المدين يجب أن يرتبط بفاتورة معتمدة.")
            if original.customer_id != self.customer_id:
                raise ValidationError("عميل الإشعار المدين يجب أن يطابق عميل الفاتورة الأصلية.")
            if self.update_stock:
                raise ValidationError("الإشعار المدين لا يحرك المخزون؛ لإرسال كميات إضافية أصدر فاتورة جديدة.")
            reason = self.remarks.replace(f"إشعار مدين على الفاتورة {original.name}:", "").strip()
            if not reason:
                raise ValidationError("اكتب سبب الإشعار المدين في الملاحظات (مطلوب في الفوترة الإلكترونية).")
        if self.is_pos:
            if not self.pos_shift_id or not self.pos_shift.is_open:
                raise ValidationError("لا توجد وردية مفتوحة لهذه الفاتورة.")
            if self.is_return:
                if self.paid_cash > 0 or self.paid_card > 0 or self.change_amount:
                    raise ValidationError("مبالغ الرد في المرتجع يجب أن تكون سالبة.")
                if flt(self.paid_cash + self.paid_card) != self.grand_total:
                    raise ValidationError("مبلغ الرد يجب أن يساوي قيمة المرتجع.")
                if self.return_against and not self.return_against.is_pos:
                    raise ValidationError("مرتجع نقطة البيع يكون لفواتير نقطة البيع فقط.")
                return
            if self.paid_cash < 0 or self.paid_card < 0:
                raise ValidationError("مبالغ الدفع لا يمكن أن تكون سالبة.")
            if self.paid_card > self.grand_total:
                raise ValidationError("مبلغ البطاقة لا يمكن أن يتجاوز إجمالي الفاتورة.")
            if flt(self.paid_cash + self.paid_card) < self.grand_total:
                raise ValidationError("المبلغ المدفوع أقل من إجمالي الفاتورة.")

    @property
    def linked_documents(self):
        docs = [doc for doc in (self.quotation, self.order, self.debit_note_against) if doc is not None]
        return docs + list(self.debit_notes.exclude(docstatus=2)) if self.pk else docs

    def on_submit(self):
        update_order_billing(self, +1)
        update_fulfillment_billing(self, "delivery_note_item", +1)
        if self.quotation_id:
            self.quotation.refresh_status()
        company = self.company
        entries = [{
            "account": company.require_account("default_receivable_account"),
            "debit": self.grand_total, "customer": self.customer,
        }]
        income_by_account, cogs_total = {}, flt(0)
        for row in self.items.select_related("item", "item__income_account"):
            income = row.item.income_account or company.require_account("default_income_account")
            income_by_account[income] = income_by_account.get(income, flt(0)) + row.amount
            if self.update_stock and row.item.is_stock_item:
                rate = self._original_cost(row.item) if self.is_return else None
                cogs_total += -make_stock_ledger_entry(self, row.item, self.warehouse, -row.stock_qty, rate)
        # الإيراد = الصافي قبل الضريبة + الخصم (قبل الضريبة)، موزعاً على حسابات الإيراد
        discount = self.discount_excluding_tax
        for account, amount in allocate(self.net_total + discount, income_by_account).items():
            entries.append({"account": account, "credit": amount})
        if discount:
            entries.append({"account": company.require_account("default_discount_account"), "debit": discount})
        if self.tax_amount:
            entries.append({"account": company.require_account("default_output_tax_account"), "credit": self.tax_amount})
        if cogs_total:
            # تكلفة المخزون محسوبة أصلاً بعملة الشركة فلا تُحوّل
            entries.append({"account": company.require_account("default_cogs_account"), "debit": cogs_total, "base": True})
            entries.append({"account": company.require_account("default_stock_account"), "credit": cogs_total, "base": True})
        default_remarks = (f"مرتجع مبيعات من الفاتورة {self.return_against}" if self.is_return
                           else f"إشعار مدين على الفاتورة {self.debit_note_against}" if self.is_debit_note
                           else f"فاتورة مبيعات للعميل {self.customer}")
        make_gl_entries(self, entries, remarks=self.remarks or default_remarks, exchange_rate=self.exchange_rate)
        self.apply_return_to_original()
        if self.is_pos:
            self._post_pos_payment()
        from einvoice.services import on_invoice_submitted

        on_invoice_submitted(self)

    def _original_cost(self, item):
        """تكلفة الصنف كما خرج في الفاتورة الأصلية، ليعود المخزون بنفس التكلفة."""
        from stock.models import StockLedgerEntry

        if not self.return_against_id:
            return None
        agg = StockLedgerEntry.objects.filter(
            voucher_type=self.doctype_slug, voucher_id=self.return_against_id, item=item, is_cancelled=False,
        ).aggregate(qty=Sum("actual_qty"), value=Sum("stock_value_difference"))
        return agg["value"] / agg["qty"] if agg["qty"] else None

    def _post_pos_payment(self):
        profile = self.pos_shift.profile
        make_gl_entries(self, [
            {"account": profile.cash_account, "debit": self.cash_received},
            {"account": profile.card_account, "debit": self.paid_card},
            {"account": self.company.require_account("default_receivable_account"),
             "credit": self.grand_total, "customer": self.customer},
        ], remarks=f"{'رد مرتجع' if self.is_return else 'تحصيل'} نقطة البيع - {self.pos_shift}")
        self.outstanding_amount = 0
        self.save(update_fields=["outstanding_amount"])

    def before_cancel(self):
        from einvoice.services import assert_cancellable

        assert_cancellable(self)
        super().before_cancel()
        if self.debit_notes.filter(docstatus=1).exists():
            raise ValidationError("لا يمكن إلغاء الفاتورة لوجود إشعارات مدينة معتمدة عليها. ألغِها أولاً.")
        if self.is_pos and not self.pos_shift.is_open:
            raise ValidationError("لا يمكن إلغاء فاتورة نقطة بيع من وردية مغلقة.")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        self.unapply_return_from_original()
        update_order_billing(self, -1)
        update_fulfillment_billing(self, "delivery_note_item", -1)
        if self.quotation_id:
            self.quotation.refresh_status()
        if self.is_pos:
            self.outstanding_amount = self.grand_total
            self.save(update_fields=["outstanding_amount"])


class SalesInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(SalesInvoice, models.CASCADE, related_name="items")
    order_item = models.ForeignKey(SalesOrderItem, models.PROTECT, null=True, blank=True, editable=False,
                                   related_name="invoice_items", verbose_name="بند أمر البيع")
    delivery_note_item = models.ForeignKey("selling.DeliveryNoteItem", models.PROTECT, null=True, blank=True,
                                           editable=False, related_name="invoice_items", verbose_name="بند إذن التسليم")

    class Meta:
        verbose_name = "بند فاتورة مبيعات"
        verbose_name_plural = "الأصناف"
        ordering = ["id"]


class DeliveryNote(FulfillmentDocument):
    """إذن تسليم: يُخرج البضاعة للعميل ويقيّد تكلفتها، وتُصدر الفاتورة منه لاحقاً."""

    naming_prefix = "DN"
    party_field = "customer"
    invoice_link_field = "delivery_note_item"

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    order = models.ForeignKey(SalesOrder, models.PROTECT, null=True, blank=True, related_name="delivery_notes",
                              editable=False, verbose_name="أمر البيع")

    class Meta(FulfillmentDocument.Meta):
        verbose_name = "إذن تسليم"
        verbose_name_plural = "أذون التسليم"

    def invoice_model(self):
        return SalesInvoice

    @property
    def linked_documents(self):
        invoices = SalesInvoice.objects.filter(items__delivery_note_item__parent=self).distinct()
        return ([self.order] if self.order_id else []) + list(invoices)

    def on_submit(self):
        cost = flt(0)
        for row in self.items.select_related("item"):
            cost += -make_stock_ledger_entry(self, row.item, self.warehouse, -row.stock_qty)
        if cost:
            company = self.company
            make_gl_entries(self, [
                {"account": company.require_account("default_cogs_account"), "debit": cost},
                {"account": company.require_account("default_stock_account"), "credit": cost},
            ], remarks=self.remarks or f"تسليم بضاعة للعميل {self.customer}")
        self.update_order_delivery(+1)

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        self.update_order_delivery(-1)


class DeliveryNoteItem(FulfillmentItem):
    parent = models.ForeignKey(DeliveryNote, models.CASCADE, related_name="items")
    order_item = models.ForeignKey(SalesOrderItem, models.PROTECT, null=True, blank=True, editable=False,
                                   related_name="delivery_items", verbose_name="بند أمر البيع")

    class Meta:
        verbose_name = "بند إذن تسليم"
        verbose_name_plural = "الأصناف"
        ordering = ["id"]


class RecurrenceFrequency(models.TextChoices):
    WEEKLY = "weekly", "أسبوعياً"
    MONTHLY = "monthly", "شهرياً"
    QUARTERLY = "quarterly", "كل 3 أشهر"
    SEMIANNUAL = "semiannual", "كل 6 أشهر"
    YEARLY = "yearly", "سنوياً"


FREQUENCY_MONTHS = {RecurrenceFrequency.MONTHLY: 1, RecurrenceFrequency.QUARTERLY: 3,
                    RecurrenceFrequency.SEMIANNUAL: 6, RecurrenceFrequency.YEARLY: 12}


def add_period(date, frequency, count=1):
    """تاريخ الدورة التالية؛ اليوم 31 يصبح آخر يوم في الأشهر الأقصر."""
    import calendar
    import datetime

    if frequency == RecurrenceFrequency.WEEKLY:
        return date + datetime.timedelta(weeks=count)
    months = date.month - 1 + FREQUENCY_MONTHS[frequency] * count
    year, month = date.year + months // 12, months % 12 + 1
    return datetime.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


class RecurringInvoice(TenantModel):
    """فاتورة متكررة (إيجار، عقد صيانة، اشتراك): تُنسخ من فاتورة نموذجية في كل دورة تلقائياً."""

    name = models.CharField("اسم العقد / الاشتراك", max_length=150)
    template_invoice = models.ForeignKey(SalesInvoice, models.PROTECT, related_name="+", verbose_name="الفاتورة النموذجية",
                                         limit_choices_to={"is_return": False, "is_debit_note": False},
                                         help_text="يُنسخ منها العميل والأصناف والأسعار والضريبة والخصم في كل دورة")
    frequency = models.CharField("التكرار", max_length=12, choices=RecurrenceFrequency.choices,
                                 default=RecurrenceFrequency.MONTHLY)
    start_date = models.DateField("تاريخ أول فاتورة")
    end_date = models.DateField("تاريخ الانتهاء", null=True, blank=True, help_text="فارغ = مستمر حتى الإيقاف")
    next_date = models.DateField("تاريخ الفاتورة القادمة", null=True, blank=True, editable=False)
    due_days = models.PositiveSmallIntegerField("أيام السداد", default=0,
                                                help_text="تاريخ الاستحقاق = تاريخ الفاتورة + هذه الأيام (0 = بلا تاريخ استحقاق)")
    auto_submit = models.BooleanField("اعتماد الفاتورة تلقائياً", default=True,
                                      help_text="وإلا تُنشأ مسودة للمراجعة ويصل تنبيه")
    enabled = models.BooleanField("مفعّل", default=True)
    generated_count = models.PositiveIntegerField("عدد الفواتير المنشأة", default=0, editable=False)
    last_invoice = models.ForeignKey(SalesInvoice, models.SET_NULL, null=True, blank=True, related_name="+",
                                     editable=False, verbose_name="آخر فاتورة")
    last_error = models.CharField("آخر خطأ", max_length=255, blank=True, editable=False)

    class Meta:
        verbose_name = "فاتورة متكررة"
        verbose_name_plural = "الفواتير المتكررة"
        ordering = ["next_date", "name"]
        constraints = [tenant_unique("name", model_label="recurringinvoice")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("doc_detail", args=["recurring-invoice", self.pk])

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ الانتهاء قبل تاريخ البداية."})
        if self.template_invoice_id and self.template_invoice.is_draft and self.template_invoice.items.count() == 0:
            raise ValidationError({"template_invoice": "الفاتورة النموذجية بلا أصناف."})

    def save(self, *args, **kwargs):
        if self.next_date is None or (self.generated_count == 0 and self.start_date):
            self.next_date = self.start_date
        super().save(*args, **kwargs)

    @property
    def is_finished(self):
        return bool(self.end_date and self.next_date and self.next_date > self.end_date)

    def invoices(self):
        return SalesInvoice.objects.filter(recurring=self).order_by("-posting_date", "-id")

    def make_invoice(self, date, user=None):
        """نسخ الفاتورة النموذجية بتاريخ الدورة واعتمادها إن طُلب."""
        import datetime

        from core.registry import get_doctype

        dt = get_doctype("sales-invoice")
        template = self.template_invoice
        skip = {"posting_date", "due_date", "exchange_rate", "remarks"}
        values = {f: getattr(template, f) for f in dt.form_fields if f not in skip}
        invoice = SalesInvoice(company=self.company, posting_date=date, recurring=self, created_by=user,
                               due_date=date + datetime.timedelta(days=self.due_days) if self.due_days else None,
                               remarks=f"{self.name} — {date:%Y-%m-%d}", **values)
        invoice.save()
        for row in template.items.all():
            invoice.items.create(**{f: getattr(row, f) for f in dt.child.fields})
        invoice.calculate_totals()
        invoice.save()
        if self.auto_submit:
            invoice.submit()
        return invoice

    def generate_due(self, today, user=None):
        """ينشئ كل الفواتير المستحقة حتى اليوم (مع اللحاق بالدورات الفائتة). يعيد الفواتير المنشأة."""
        from django.db import transaction

        created = []
        while self.enabled and self.next_date and self.next_date <= today and not self.is_finished:
            date = self.next_date
            try:
                with transaction.atomic():
                    invoice = self.make_invoice(date, user)
            except ValidationError as exc:
                # الاعتماد فشل (حد ائتماني، سنة مالية…): تبقى مسودة للمراجعة دون إيقاف الدورات
                self.last_error = " ".join(exc.messages)[:255]
                with transaction.atomic():
                    auto, self.auto_submit = self.auto_submit, False
                    invoice = self.make_invoice(date, user)
                    self.auto_submit = auto
            else:
                self.last_error = ""
            created.append(invoice)
            self.last_invoice = invoice
            self.generated_count += 1
            self.next_date = add_period(self.start_date, self.frequency, self.generated_count)
            self.save(update_fields=["last_invoice", "generated_count", "next_date", "last_error"])
        return created
