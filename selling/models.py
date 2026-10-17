from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone

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


class CustomerGroup(TenantModel):
    """فئة العملاء (جملة، تجزئة، كبار العملاء...): قائمة أسعار افتراضية ونطاق لقواعد التسعير."""

    name = models.CharField("اسم الفئة", max_length=100)
    price_list = models.ForeignKey("pricing.PriceList", models.SET_NULL, null=True, blank=True, related_name="+",
                                   limit_choices_to={"kind": "selling", "disabled": False},
                                   verbose_name="قائمة الأسعار الافتراضية")

    class Meta:
        verbose_name = "فئة عملاء"
        verbose_name_plural = "فئات العملاء"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="customergroup")]

    def __str__(self):
        return self.name


class Territory(TenantModel):
    """منطقة بيع (شجرة: المنطقة الوسطى ← الرياض ← شمال الرياض)."""

    name = models.CharField("اسم المنطقة", max_length=100)
    parent = models.ForeignKey("self", models.PROTECT, null=True, blank=True, related_name="children",
                               verbose_name="المنطقة الأم")

    class Meta:
        verbose_name = "منطقة بيع"
        verbose_name_plural = "مناطق البيع"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="territory")]

    def __str__(self):
        return self.name

    def descendant_ids(self):
        ids, stack = [], [self]
        while stack:
            node = stack.pop()
            ids.append(node.id)
            stack.extend(node.children.all())
        return ids


class SalesPerson(TenantModel):
    """مندوب مبيعات بنسبة عمولة، ويمكن ربطه بموظف لإضافة عمولته لمسير الرواتب."""

    name = models.CharField("اسم المندوب", max_length=100)
    employee = models.ForeignKey("hr.Employee", models.SET_NULL, null=True, blank=True, related_name="+",
                                 verbose_name="الموظف")
    territory = models.ForeignKey(Territory, models.SET_NULL, null=True, blank=True, related_name="sales_persons",
                                  verbose_name="المنطقة")
    commission_rate = models.DecimalField("نسبة العمولة %", max_digits=5, decimal_places=2, default=0,
                                          help_text="من صافي المبيعات قبل الضريبة")
    monthly_target = models.DecimalField("المستهدف الشهري", max_digits=18, decimal_places=2, default=0)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "مندوب مبيعات"
        verbose_name_plural = "مندوبو المبيعات"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="salesperson")]

    def __str__(self):
        return self.name


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
    customer_group = models.ForeignKey(CustomerGroup, models.SET_NULL, null=True, blank=True, related_name="customers",
                                       verbose_name="فئة العميل")
    price_list = models.ForeignKey("pricing.PriceList", models.SET_NULL, null=True, blank=True, related_name="+",
                                   limit_choices_to={"kind": "selling", "disabled": False},
                                   verbose_name="قائمة أسعار خاصة", help_text="فارغة = قائمة فئة العميل أو الافتراضية")
    loyalty_program = models.ForeignKey("pricing.LoyaltyProgram", models.SET_NULL, null=True, blank=True,
                                        related_name="+", verbose_name="برنامج الولاء")
    payment_terms = models.ForeignKey("accounts.PaymentTermsTemplate", models.SET_NULL, null=True, blank=True,
                                      related_name="+", verbose_name="شروط الدفع الافتراضية")
    sales_person = models.ForeignKey(SalesPerson, models.SET_NULL, null=True, blank=True, related_name="customers",
                                     verbose_name="المندوب المسؤول")
    territory = models.ForeignKey(Territory, models.SET_NULL, null=True, blank=True, related_name="customers",
                                  verbose_name="المنطقة")
    represents_company = models.ForeignKey("core.Company", models.SET_NULL, null=True, blank=True, related_name="+",
                                           verbose_name="شركة شقيقة (معاملات بين الشركات)",
                                           help_text="فواتير هذا العميل تُنشئ فاتورة مشتريات مسودة في الشركة الشقيقة، "
                                                     "وتُستبعد في القوائم الموحدة")
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

    @property
    def loyalty_points(self):
        from pricing.engine import points_balance, program_for

        return points_balance(self) if program_for(self) else None


class Quotation(CommercialDocument):
    naming_prefix = "QTN"

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    valid_till = models.DateField("صالح حتى", null=True, blank=True)
    sales_person = models.ForeignKey(SalesPerson, models.PROTECT, null=True, blank=True, related_name="+",
                                     verbose_name="المندوب", help_text="فارغ = مندوب العميل")

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


class SalesOrder(OrderDocument):
    naming_prefix = "SO"

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    delivery_date = models.DateField("تاريخ التسليم المتوقع", null=True, blank=True)
    sales_person = models.ForeignKey(SalesPerson, models.PROTECT, null=True, blank=True, related_name="+",
                                     verbose_name="المندوب", help_text="فارغ = مندوب العميل")
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


class SalesInvoice(InvoiceDocument):
    naming_prefix = "SINV"
    return_prefix = "SRET"
    party_field = "customer"
    return_locked_fields = InvoiceDocument.return_locked_fields + ["prices_include_tax", "customer", "sales_person",
                                                                   "commission_rate"]

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    prices_include_tax = models.BooleanField("الأسعار شاملة الضريبة", default=False)
    sales_person = models.ForeignKey(SalesPerson, models.PROTECT, null=True, blank=True, related_name="+",
                                     verbose_name="المندوب", help_text="فارغ = مندوب العميل")
    commission_rate = models.DecimalField("نسبة العمولة %", max_digits=5, decimal_places=2, default=0, editable=False)
    commission_amount = models.DecimalField("العمولة", max_digits=18, decimal_places=2, default=0, editable=False)
    order = models.ForeignKey(SalesOrder, models.PROTECT, null=True, blank=True, related_name="invoices",
                              editable=False, verbose_name="أمر البيع")
    quotation = models.ForeignKey(Quotation, models.PROTECT, null=True, blank=True, related_name="invoices",
                                  editable=False, verbose_name="عرض السعر")

    # حقول نقاط البيع
    is_pos = models.BooleanField("فاتورة نقطة بيع", default=False, editable=False)
    pos_shift = models.ForeignKey(
        "pos.POSShift", models.PROTECT, null=True, blank=True, related_name="invoices", editable=False, verbose_name="الوردية"
    )
    pos_credit_sale = models.BooleanField("بيع آجل من نقطة البيع", default=False, editable=False)
    paid_cash = models.DecimalField("المدفوع نقداً", max_digits=18, decimal_places=2, default=0, editable=False)
    paid_card = models.DecimalField("المدفوع بالبطاقة", max_digits=18, decimal_places=2, default=0, editable=False)
    change_amount = models.DecimalField("المبلغ المرتجع (الباقي)", max_digits=18, decimal_places=2, default=0, editable=False)
    # معرّف البيع لدى جهاز نقطة البيع عند البيع دون اتصال؛ يمنع تكرار الفاتورة عند إعادة المزامنة
    offline_id = models.CharField("معرّف البيع دون اتصال", max_length=64, blank=True, editable=False, db_index=True)
    offline_at = models.DateTimeField("وقت البيع دون اتصال", null=True, blank=True, editable=False)

    # الإشعار المدين: زيادة على فاتورة سابقة (فرق سعر، رسوم إضافية...) دون حركة مخزون
    loyalty_points = models.PositiveIntegerField("نقاط ولاء مستبدلة", default=0, blank=True,
                                                 help_text="تُخصم قيمتها من المستحق على العميل")
    loyalty_amount = models.DecimalField("قيمة نقاط الولاء", max_digits=18, decimal_places=2, default=0, editable=False)
    loyalty_points_earned = models.IntegerField("نقاط ولاء مكتسبة", default=0, editable=False)

    is_debit_note = models.BooleanField("إشعار مدين", default=False, editable=False)
    debit_note_against = models.ForeignKey("self", models.PROTECT, null=True, blank=True, related_name="debit_notes",
                                           editable=False, verbose_name="إشعار مدين على الفاتورة")

    class Meta(InvoiceDocument.Meta):
        verbose_name = "فاتورة مبيعات"
        verbose_name_plural = "فواتير المبيعات"

    @property
    def cash_received(self):
        return flt(self.paid_cash - self.change_amount)

    @property
    def amount_due(self):
        """ما يُدفع فعلياً: الإجمالي ناقص قيمة نقاط الولاء المستبدلة."""
        return flt(self.grand_total - self.loyalty_amount)

    def calculate_totals(self):
        super().calculate_totals()
        if self.is_draft and not self.is_return:
            if not self.sales_person_id and self.customer_id and self.customer.sales_person_id:
                self.sales_person = self.customer.sales_person
            self.commission_rate = self.sales_person.commission_rate if self.sales_person_id else flt(0)
        # العمولة من الصافي بعملة الشركة (والمرتجع يخصم عمولته)
        self.commission_amount = flt(self.base_net_total * flt(self.commission_rate) / 100)
        self.loyalty_amount = flt(0)
        if self.loyalty_points and not self.is_return:
            from pricing.engine import program_for

            program = program_for(self.customer) if self.customer_id else None
            if program is not None:
                self.loyalty_amount = flt(self.loyalty_points * program.point_value)
        if self.is_draft:
            self.outstanding_amount = self.amount_due

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
            check_credit(self, self.to_base(max(self.amount_due - paid, flt(0))))
        self._validate_loyalty()
        if self.update_stock and not self.is_return:
            from stock.reservation import check_reserved

            check_reserved(self, list(self.items.select_related("item")), self.warehouse)
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
                applied = min(max(self.return_against.outstanding_amount, flt(0)), -self.grand_total) if self.return_against_id else flt(0)
                if flt(self.paid_cash + self.paid_card) != flt(self.grand_total + applied):
                    raise ValidationError("مبلغ الرد يجب أن يساوي قيمة المرتجع بعد تسوية المتبقي على الفاتورة الأصلية.")
                if self.return_against and not self.return_against.is_pos:
                    raise ValidationError("مرتجع نقطة البيع يكون لفواتير نقطة البيع فقط.")
                return
            if self.paid_cash < 0 or self.paid_card < 0:
                raise ValidationError("مبالغ الدفع لا يمكن أن تكون سالبة.")
            if self.paid_card > self.amount_due:
                raise ValidationError("مبلغ البطاقة لا يمكن أن يتجاوز إجمالي الفاتورة.")
            if self.pos_credit_sale:
                if (self.customer_id == self.pos_shift.profile.default_customer_id or self.customer.disabled
                        or self.customer.company_id != self.company_id):
                    raise ValidationError("البيع الآجل متاح لعميل مسجل من قائمة العملاء، ولا يشمل العميل النقدي الافتراضي.")
            elif flt(self.paid_cash + self.paid_card) < self.amount_due:
                raise ValidationError("المبلغ المدفوع أقل من إجمالي الفاتورة.")

    def _validate_loyalty(self):
        if not self.loyalty_points:
            return
        from pricing.engine import points_balance, program_for

        program = program_for(self.customer)
        if self.is_return or self.is_debit_note:
            raise ValidationError("لا تُستبدل نقاط الولاء في المرتجعات والإشعارات.")
        if program is None:
            raise ValidationError(f"العميل «{self.customer}» غير مسجل في برنامج ولاء.")
        if self.is_foreign_currency:
            raise ValidationError("استبدال نقاط الولاء متاح للفواتير بعملة الشركة فقط.")
        if self.loyalty_points < program.min_redeem_points:
            raise ValidationError(f"أقل عدد نقاط للاستبدال {program.min_redeem_points}.")
        balance = points_balance(self.customer, self.posting_date)
        if self.loyalty_points > balance:
            raise ValidationError(f"رصيد نقاط العميل {balance} فقط.")
        if self.loyalty_amount > self.grand_total:
            raise ValidationError("قيمة النقاط المستبدلة أكبر من إجمالي الفاتورة.")

    def _post_loyalty(self):
        from pricing.engine import program_for
        from pricing.models import LoyaltyPointEntry

        program = program_for(self.customer) if not self.is_debit_note else None
        if program is None:
            return
        if self.loyalty_points and self.loyalty_amount:
            make_gl_entries(self, [
                {"account": program.expense_account or self.company.require_account("default_discount_account"),
                 "debit": self.loyalty_amount},
                {"account": self.company.require_account("default_receivable_account"),
                 "credit": self.loyalty_amount, "customer": self.customer},
            ], remarks=f"استبدال {self.loyalty_points} نقطة ولاء")
            LoyaltyPointEntry.objects.create(customer=self.customer, program=program, invoice=self,
                                             points=-self.loyalty_points, posting_date=self.posting_date)
        # النقاط على ما دُفع فعلاً (بعملة الشركة)، والمرتجع يخصم نقاطه
        basis = abs(flt(self.base_grand_total)) - flt(self.loyalty_amount)
        points = int(basis // flt(program.amount_per_point)) if basis > 0 else 0
        if points:
            points = -points if self.is_return else points
            expiry = (self.posting_date + timezone.timedelta(days=program.expiry_days)
                      if program.expiry_days and points > 0 else None)
            LoyaltyPointEntry.objects.create(customer=self.customer, program=program, invoice=self, points=points,
                                             posting_date=self.posting_date, expiry_date=expiry)
        self.loyalty_points_earned = points
        self.save(update_fields=["loyalty_points_earned"])

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
                cogs_total += -make_stock_ledger_entry(self, row.item, self.warehouse, -row.stock_qty, rate,
                                                       serial_nos=row.serial_nos)
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
        from pricing.engine import consume_coupons

        consume_coupons(self, +1)
        self._post_loyalty()
        if self.is_pos:
            self._post_pos_payment()
        elif self.loyalty_amount:
            self.outstanding_amount = self.amount_due
            self.save(update_fields=["outstanding_amount"])
        from einvoice.services import on_invoice_submitted

        on_invoice_submitted(self)
        if self.customer.represents_company_id and not self.is_pos:
            from core.intercompany import mirror_sales_invoice

            mirror_sales_invoice(self)

    def _original_cost(self, item):
        """تكلفة الصنف كما خرج في الفاتورة الأصلية، ليعود المخزون بنفس التكلفة."""
        from stock.models import StockLedgerEntry

        if not self.return_against_id:
            return None
        agg = StockLedgerEntry.objects.filter(
            voucher_type=self.doctype_slug, voucher_id=self.return_against_id, item=item, is_cancelled=False,
        ).aggregate(qty=Sum("actual_qty"), value=Sum("stock_value_difference"))
        from stock.negative import cost_corrections
        correction = cost_corrections(self.doctype_slug, [self.return_against_id]).get((self.return_against_id, item.pk), 0)
        return (agg["value"] - correction) / agg["qty"] if agg["qty"] else None

    def _post_pos_payment(self):
        profile = self.pos_shift.profile
        make_gl_entries(self, [
            {"account": profile.cash_account, "debit": self.cash_received},
            {"account": profile.card_account, "debit": self.paid_card},
            {"account": self.company.require_account("default_receivable_account"),
             "credit": self.cash_received + self.paid_card, "customer": self.customer},
        ], remarks=f"{'رد مرتجع' if self.is_return else 'تحصيل'} نقطة البيع - {self.pos_shift}")
        self.outstanding_amount = flt(self.amount_due + (self.applied_to_original if self.is_return else 0) - self.cash_received - self.paid_card)
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
        from pricing.engine import consume_coupons

        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        consume_coupons(self, -1)
        self.loyalty_entries.all().delete()
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

    def validate_for_submit(self):
        super().validate_for_submit()
        from stock.reservation import check_reserved

        check_reserved(self, list(self.items.select_related("item")), self.warehouse)

    @property
    def linked_documents(self):
        invoices = SalesInvoice.objects.filter(items__delivery_note_item__parent=self).distinct()
        return ([self.order] if self.order_id else []) + list(invoices)

    def on_submit(self):
        cost = flt(0)
        for row in self.items.select_related("item"):
            cost += -make_stock_ledger_entry(self, row.item, self.warehouse, -row.stock_qty, serial_nos=row.serial_nos)
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
