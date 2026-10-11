from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.invoice import CurrencyMixin
from core.models import SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class RootType(models.TextChoices):
    ASSET = "asset", "أصول"
    LIABILITY = "liability", "خصوم"
    EQUITY = "equity", "حقوق الملكية"
    INCOME = "income", "إيرادات"
    EXPENSE = "expense", "مصروفات"


class AccountType(models.TextChoices):
    CASH = "cash", "نقدية"
    BANK = "bank", "بنك"
    RECEIVABLE = "receivable", "مدينون (عملاء)"
    PAYABLE = "payable", "دائنون (موردون)"
    STOCK = "stock", "مخزون"
    TAX = "tax", "ضريبة"
    FIXED_ASSET = "fixed_asset", "أصل ثابت"
    ACCUMULATED_DEPRECIATION = "accumulated_depreciation", "مجمع إهلاك"
    COGS = "cogs", "تكلفة البضاعة المباعة"
    STOCK_ADJUSTMENT = "stock_adjustment", "تسوية مخزون"
    INCOME = "income_account", "حساب إيراد"
    EXPENSE = "expense_account", "حساب مصروف"
    EQUITY = "equity", "حقوق ملكية"


class CashFlowCategory(models.TextChoices):
    OPERATING = "operating", "أنشطة تشغيلية"
    INVESTING = "investing", "أنشطة استثمارية"
    FINANCING = "financing", "أنشطة تمويلية"


class ZakatTreatment(models.TextChoices):
    ADD = "add", "يُضاف للوعاء"
    DEDUCT = "deduct", "يُحسم من الوعاء"
    IGNORE = "ignore", "لا يدخل في الوعاء"


class Account(TenantModel):
    number = models.CharField("رقم الحساب", max_length=20, blank=True)
    name = models.CharField("اسم الحساب", max_length=150)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children", verbose_name="الحساب الأب"
    )
    is_group = models.BooleanField("حساب رئيسي (مجموعة)", default=False)
    root_type = models.CharField("النوع الجذري", max_length=20, choices=RootType.choices)
    account_type = models.CharField("نوع الحساب", max_length=30, choices=AccountType.choices, blank=True)
    account_currency = models.ForeignKey(
        "core.Currency", models.PROTECT, null=True, blank=True, related_name="+", verbose_name="عملة الحساب",
        help_text="لحسابات البنك والصندوق بعملة أجنبية فقط (حساب بالدولار مثلاً). يُترك فارغاً لعملة الشركة")
    cash_flow_category = models.CharField(
        "التصنيف في قائمة التدفقات النقدية", max_length=10, choices=CashFlowCategory.choices, blank=True,
        help_text="فارغ = تلقائي: الأصول الثابتة استثمارية، وحقوق الملكية والقروض تمويلية، والباقي تشغيلية")
    zakat_treatment = models.CharField(
        "المعالجة في الوعاء الزكوي", max_length=10, choices=ZakatTreatment.choices, blank=True,
        help_text="فارغ = تلقائي: حقوق الملكية والخصوم غير المتداولة تُضاف، والأصول غير المتداولة تُحسم، والمتداولة لا تدخل")
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "حساب"
        verbose_name_plural = "دليل الحسابات"
        ordering = ["number", "name"]
        constraints = [tenant_unique("name", model_label="account")]

    def __str__(self):
        return f"{self.number} - {self.name}" if self.number else self.name

    @property
    def balance_must_be_debit(self):
        return self.root_type in (RootType.ASSET, RootType.EXPENSE)

    @property
    def is_foreign_currency(self):
        return bool(self.account_currency_id) and self.account_currency_id != self.company.currency_id

    @property
    def cash_flow_section(self):
        """تصنيف الحساب في قائمة التدفقات النقدية (المحدد يدوياً، وإلا حسب نوعه)."""
        if self.cash_flow_category:
            return self.cash_flow_category
        if self.account_type == AccountType.FIXED_ASSET:
            return CashFlowCategory.INVESTING
        if self.root_type == RootType.EQUITY:
            return CashFlowCategory.FINANCING
        if self.root_type == RootType.LIABILITY and any(word in self.name for word in ("قرض", "قروض", "تمويل")):
            return CashFlowCategory.FINANCING
        return CashFlowCategory.OPERATING

    def clean(self):
        if self.account_currency_id and self.company_id and self.account_currency_id == self.company.currency_id:
            self.account_currency = None
        if self.account_currency_id:
            if self.is_group or self.account_type not in (AccountType.BANK, AccountType.CASH):
                raise ValidationError({"account_currency": "العملة الأجنبية متاحة لحسابات البنك والصندوق الفرعية فقط."})
        if self.pk:
            old = Account.all_objects.filter(pk=self.pk).values_list("account_currency_id", flat=True).first()
            if old != self.account_currency_id and self.gl_entries.filter(is_cancelled=False).exists():
                raise ValidationError({"account_currency": "لا يمكن تغيير عملة حساب عليه قيود."})
        if self.parent_id:
            if not self.parent.is_group:
                raise ValidationError({"parent": "الحساب الأب يجب أن يكون حساباً رئيسياً (مجموعة)."})
            if self.parent.root_type != self.root_type:
                raise ValidationError({"root_type": "النوع الجذري يجب أن يطابق نوع الحساب الأب."})
        if self.pk and not self.is_group and self.children.exists():
            raise ValidationError({"is_group": "لا يمكن تحويل حساب له حسابات فرعية إلى حساب فرعي."})
        if self.pk and self.is_group and self.gl_entries.exists():
            raise ValidationError({"is_group": "لا يمكن تحويل حساب عليه قيود إلى مجموعة."})

    def descendant_ids(self):
        ids, stack = [], [self]
        while stack:
            node = stack.pop()
            ids.append(node.id)
            stack.extend(node.children.all())
        return ids


class CostCenter(TenantModel):
    """مراكز التكلفة / الفروع: شجرة يُحلل عليها الإيراد والمصروف."""

    name = models.CharField("اسم مركز التكلفة", max_length=100)
    parent = models.ForeignKey("self", models.PROTECT, null=True, blank=True, related_name="children",
                               limit_choices_to={"is_group": True}, verbose_name="المركز الأب")
    is_group = models.BooleanField("مركز رئيسي (مجموعة)", default=False)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "مركز تكلفة"
        verbose_name_plural = "مراكز التكلفة والفروع"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="costcenter")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.parent_id and not self.parent.is_group:
            raise ValidationError({"parent": "المركز الأب يجب أن يكون مركزاً رئيسياً."})

    def descendant_ids(self):
        ids, stack = [], [self]
        while stack:
            node = stack.pop()
            ids.append(node.id)
            stack.extend(node.children.all())
        return ids


class PartyType(models.TextChoices):
    CUSTOMER = "customer", "عميل"
    SUPPLIER = "supplier", "مورد"


class GLEntry(TenantModel):
    """قيد في دفتر الأستاذ العام - يُنشأ تلقائياً عند اعتماد المستندات."""

    posting_date = models.DateField("التاريخ", db_index=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="gl_entries", verbose_name="الحساب")
    debit = models.DecimalField("مدين", max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField("دائن", max_digits=18, decimal_places=2, default=0)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    cost_center = models.ForeignKey(CostCenter, models.PROTECT, null=True, blank=True, verbose_name="مركز التكلفة")
    voucher_type = models.CharField("نوع المستند", max_length=50)
    voucher_no = models.CharField("رقم المستند", max_length=40)
    voucher_id = models.PositiveBigIntegerField("معرّف المستند")
    remarks = models.CharField("البيان", max_length=255, blank=True)
    is_cancelled = models.BooleanField("ملغى", default=False)
    clearance_date = models.DateField("تاريخ الظهور في كشف البنك", null=True, blank=True)
    # لحسابات البنك بعملة أجنبية: المبلغ بعملة الحساب بجانب قيمته بعملة الشركة
    account_currency = models.ForeignKey("core.Currency", models.PROTECT, null=True, blank=True, related_name="+",
                                         verbose_name="عملة الحساب")
    debit_in_account_currency = models.DecimalField("مدين بعملة الحساب", max_digits=18, decimal_places=2, default=0)
    credit_in_account_currency = models.DecimalField("دائن بعملة الحساب", max_digits=18, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "قيد أستاذ عام"
        verbose_name_plural = "قيود الأستاذ العام"
        ordering = ["posting_date", "id"]
        indexes = [models.Index(fields=["voucher_type", "voucher_id"]), models.Index(fields=["account", "posting_date"])]

    def __str__(self):
        return f"{self.voucher_no} | {self.account}"

    @property
    def party(self):
        return self.customer or self.supplier

    @property
    def account_amount(self):
        """المبلغ بعملة الحساب (موجب للمدين): بعملة الحساب الأجنبية إن وُجدت، وإلا بعملة الشركة."""
        if self.account_currency_id:
            return flt(self.debit_in_account_currency) - flt(self.credit_in_account_currency)
        return flt(self.debit) - flt(self.credit)


def account_currency_amount(field_label, account, amount, debit, credit):
    """المبلغ بعملة الحساب لسطر يدوي (موجب للمدين)، مطلوب فقط للحسابات بعملة أجنبية."""
    if not account.is_foreign_currency:
        return None
    if not amount or amount <= 0:
        raise ValidationError(f"الحساب {account} بعملة {account.account_currency.code}: أدخل «{field_label}».")
    return amount if debit else -amount


class JournalEntryType(models.TextChoices):
    JOURNAL = "journal", "قيد يومية"
    OPENING = "opening", "قيد افتتاحي"
    ADJUSTMENT = "adjustment", "قيد تسوية"
    DEPRECIATION = "depreciation", "قيد إهلاك"


class JournalEntry(SubmittableDocument):
    naming_prefix = "JV"

    entry_type = models.CharField("نوع القيد", max_length=20, choices=JournalEntryType.choices, default=JournalEntryType.JOURNAL)
    total_debit = models.DecimalField("إجمالي المدين", max_digits=18, decimal_places=2, default=0, editable=False)
    total_credit = models.DecimalField("إجمالي الدائن", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "قيد يومية"
        verbose_name_plural = "قيود اليومية"

    def calculate_totals(self):
        lines = list(self.accounts.all())
        self.total_debit = flt(sum(flt(line.debit) for line in lines))
        self.total_credit = flt(sum(flt(line.credit) for line in lines))

    def validate_for_submit(self):
        lines = list(self.accounts.select_related("account"))
        if len(lines) < 2:
            raise ValidationError("القيد يجب أن يحتوي على سطرين على الأقل.")
        for line in lines:
            line.validate_line()
        if self.total_debit != self.total_credit:
            raise ValidationError(
                f"القيد غير متوازن: إجمالي المدين {self.total_debit} لا يساوي إجمالي الدائن {self.total_credit}."
            )
        if not self.total_debit:
            raise ValidationError("لا يمكن اعتماد قيد بقيمة صفرية.")

    def on_submit(self):
        make_gl_entries(self, [
            {
                "account": line.account,
                "debit": line.debit,
                "credit": line.credit,
                "customer": line.customer,
                "supplier": line.supplier,
                "cost_center": line.cost_center,
                "remarks": line.description or self.remarks,
                "account_amount": account_currency_amount("المبلغ بعملة الحساب", line.account,
                                                          line.account_currency_amount, line.debit, line.credit),
            }
            for line in self.accounts.select_related("account", "customer", "supplier")
        ])

    def before_cancel(self):
        from assets.services import check_journal_cancel

        check_journal_cancel(self)

    def on_cancel(self):
        from accounts.cheques import on_journal_cancel as on_cheque_journal_cancel
        from assets.services import on_journal_cancel

        cancel_gl_entries(self)
        on_journal_cancel(self)
        on_cheque_journal_cancel(self)


class JournalEntryAccount(models.Model):
    parent = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="accounts")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, limit_choices_to={"is_group": False, "disabled": False}, verbose_name="الحساب"
    )
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    debit = models.DecimalField("مدين", max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField("دائن", max_digits=18, decimal_places=2, default=0)
    cost_center = models.ForeignKey(CostCenter, models.PROTECT, null=True, blank=True, verbose_name="مركز التكلفة",
                                    limit_choices_to={"is_group": False, "disabled": False})
    description = models.CharField("البيان", max_length=255, blank=True)
    account_currency_amount = models.DecimalField(
        "المبلغ بعملة الحساب", max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="لحسابات البنك بعملة أجنبية فقط: المبلغ بالدولار مثلاً، والمدين/الدائن بعملة الشركة")

    class Meta:
        verbose_name = "سطر قيد"
        verbose_name_plural = "سطور القيد"
        ordering = ["id"]

    def validate_line(self):
        if self.debit and self.credit:
            raise ValidationError(f"الحساب {self.account}: لا يمكن أن يكون السطر مديناً ودائناً معاً.")
        if self.debit < 0 or self.credit < 0:
            raise ValidationError(f"الحساب {self.account}: لا يُسمح بالقيم السالبة.")
        if self.account.account_type == AccountType.RECEIVABLE and not self.customer_id:
            raise ValidationError(f"الحساب {self.account} حساب عملاء ويتطلب تحديد العميل.")
        if self.account.account_type == AccountType.PAYABLE and not self.supplier_id:
            raise ValidationError(f"الحساب {self.account} حساب موردين ويتطلب تحديد المورد.")
        account_currency_amount("المبلغ بعملة الحساب", self.account, self.account_currency_amount, self.debit, self.credit)


class PaymentType(models.TextChoices):
    RECEIVE = "receive", "سند قبض"
    PAY = "pay", "سند صرف"


class PaymentMode(models.TextChoices):
    DIRECT = "direct", "نقدي / تحويل"
    CHEQUE = "cheque", "شيك"


class PaymentEntry(CurrencyMixin, SubmittableDocument):
    naming_prefix = "PAY"

    payment_type = models.CharField("نوع السند", max_length=10, choices=PaymentType.choices, default=PaymentType.RECEIVE)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, verbose_name="حساب الصندوق / البنك",
        limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH, AccountType.BANK]},
    )
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    currency = models.ForeignKey("core.Currency", models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="العملة")
    exchange_rate = models.DecimalField("سعر الصرف", max_digits=18, decimal_places=6, null=True, blank=True,
                                        help_text="يُترك فارغاً لاستخدام آخر سعر في جدول أسعار الصرف")
    base_amount = models.DecimalField("المبلغ بعملة الشركة", max_digits=18, decimal_places=2, default=0, editable=False)
    exchange_difference = models.DecimalField("فرق سعر الصرف (ربح + / خسارة −)", max_digits=18, decimal_places=2,
                                              default=0, editable=False)
    sales_invoice = models.ForeignKey(
        "selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="payments", verbose_name="فاتورة المبيعات"
    )
    purchase_invoice = models.ForeignKey(
        "buying.PurchaseInvoice", models.PROTECT, null=True, blank=True, related_name="payments", verbose_name="فاتورة المشتريات"
    )
    reference_no = models.CharField("رقم المرجع (شيك / حوالة)", max_length=50, blank=True)
    payment_mode = models.CharField(
        "طريقة الدفع", max_length=10, choices=PaymentMode.choices, default=PaymentMode.DIRECT,
        help_text="الشيك يُرحّل لحساب الشيكات تحت التحصيل (أو المستحقة الدفع) حتى يُصرف من البنك أو يرتد")
    cheque_no = models.CharField("رقم الشيك", max_length=30, blank=True)
    cheque_date = models.DateField("تاريخ استحقاق الشيك", null=True, blank=True)
    drawee_bank = models.CharField("البنك المسحوب عليه", max_length=100, blank=True)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "سند قبض / صرف"
        verbose_name_plural = "سندات القبض والصرف"

    @property
    def party(self):
        return self.customer or self.supplier

    @property
    def invoice(self):
        return self.sales_invoice if self.customer_id else self.purchase_invoice

    @property
    def direction(self):
        """+1 للاتجاه المعتاد (قبض من عميل / صرف لمورد)، و-1 للرد (صرف لعميل / قبض من مورد)."""
        normal = (self.payment_type == PaymentType.RECEIVE) == bool(self.customer_id)
        return 1 if normal else -1

    def save(self, *args, **kwargs):
        self.ensure_currency()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        self.ensure_currency()
        self.base_amount = self.to_base(self.amount)

    def validate_for_submit(self):
        self.validate_currency()
        if flt(self.amount) <= 0:
            raise ValidationError("المبلغ يجب أن يكون أكبر من صفر.")
        if self.invoice and self.invoice.currency_id != self.currency_id:
            raise ValidationError(f"عملة السند يجب أن تطابق عملة الفاتورة ({self.invoice.currency}).")
        if self.payment_mode == PaymentMode.CHEQUE:
            if not self.cheque_no or not self.cheque_date:
                raise ValidationError("أدخل رقم الشيك وتاريخ استحقاقه.")
            if self.is_foreign_currency or self.account.is_foreign_currency:
                raise ValidationError("الشيكات بعملة الشركة فقط.")
        if self.account.is_foreign_currency and self.account.account_currency_id != self.currency_id:
            raise ValidationError(f"الحساب {self.account} بعملة {self.account.account_currency.code}؛ "
                                  f"السند يجب أن يكون بنفس العملة.")
        if bool(self.customer_id) == bool(self.supplier_id):
            raise ValidationError("حدد عميلاً أو مورداً واحداً فقط.")
        if self.customer_id and self.purchase_invoice_id or self.supplier_id and self.sales_invoice_id:
            raise ValidationError("الفاتورة المرجعية لا تتوافق مع نوع الطرف.")
        invoice, party_field = self.invoice, "customer_id" if self.customer_id else "supplier_id"
        if invoice:
            if not invoice.is_submitted:
                raise ValidationError("الفاتورة المرجعية غير معتمدة.")
            if getattr(invoice, party_field) != getattr(self, party_field):
                raise ValidationError("الفاتورة المرجعية لا تخص الطرف المحدد.")
            available = flt(invoice.outstanding_amount) * self.direction
            if available <= 0:
                raise ValidationError("لا يوجد رصيد مستحق على الفاتورة المرجعية في هذا الاتجاه.")
            if flt(self.amount) > available:
                raise ValidationError(f"المبلغ أكبر من المتبقي على الفاتورة ({available}).")

    def on_submit(self):
        amount = flt(self.amount)
        if self.customer_id:
            party_account = self.company.require_account("default_receivable_account")
        else:
            party_account = self.company.require_account("default_payable_account")
        party = {"customer": self.customer, "supplier": self.supplier}
        # النقدية بسعر صرف السند، والذمة بسعر صرف الفاتورة التي نشأت بها؛ الفرق ربح أو خسارة صرف
        cash_base = self.to_base(amount)
        party_base = flt(amount * (self.invoice.exchange_rate if self.invoice else self.exchange_rate))
        cash_account = self.account
        if self.payment_mode == PaymentMode.CHEQUE:
            from accounts.cheques import cheques_account

            # الشيك يبقى في حساب وسيط حتى يُصرف فعلاً من البنك
            cash_account = cheques_account(self.company, self.payment_type == PaymentType.RECEIVE)
        if self.payment_type == PaymentType.RECEIVE:
            entries = [
                {"account": cash_account, "debit": cash_base, "base": True, "account_amount": amount},
                {"account": party_account, "credit": party_base, "base": True, **party},
            ]
            gain = cash_base - party_base
        else:
            entries = [
                {"account": party_account, "debit": party_base, "base": True, **party},
                {"account": cash_account, "credit": cash_base, "base": True, "account_amount": -amount},
            ]
            gain = party_base - cash_base
        if gain:
            fx_account = self.company.require_account("default_exchange_account")
            entries.append({"account": fx_account, "credit": gain if gain > 0 else 0, "debit": -gain if gain < 0 else 0,
                            "base": True, "remarks": "فرق سعر الصرف"})
        self.exchange_difference = gain
        self.save(update_fields=["exchange_difference"])
        make_gl_entries(self, entries, remarks=self.remarks or self.get_payment_type_display())
        self._update_invoice_outstanding(-amount * self.direction)
        if self.payment_mode == PaymentMode.CHEQUE:
            Cheque.objects.update_or_create(payment=self, defaults={
                "company": self.company, "status": ChequeStatus.PENDING, "status_date": None,
                "clearance_entry": None, "bounce_entry": None,
            })

    def before_cancel(self):
        cheque = getattr(self, "cheque", None)
        if cheque is not None and cheque.status != ChequeStatus.PENDING:
            raise ValidationError(f"الشيك {cheque.cheque_no} {cheque.get_status_display()}؛ ألغِ قيد الصرف أو الارتداد أولاً.")

    def on_cancel(self):
        cancel_gl_entries(self)
        self._update_invoice_outstanding(flt(self.amount) * self.direction)
        Cheque.objects.filter(payment=self).update(status=ChequeStatus.CANCELLED, status_date=self.posting_date)

    def _update_invoice_outstanding(self, delta):
        invoice = self.invoice
        if invoice:
            invoice.outstanding_amount = flt(invoice.outstanding_amount + delta)
            invoice.save(update_fields=["outstanding_amount"])


class OpeningAccountBalance(SubmittableDocument):
    """أرصدة الحسابات الافتتاحية (البنوك، الأصول، القروض، رأس المال...). الفرق يُرحّل لحساب الأرصدة الافتتاحية."""

    naming_prefix = "OPA"

    class Meta(SubmittableDocument.Meta):
        verbose_name = "أرصدة افتتاحية للحسابات"
        verbose_name_plural = "الأرصدة الافتتاحية للحسابات"

    def calculate_totals(self):
        pass

    def validate_for_submit(self):
        lines = list(self.lines.select_related("account"))
        if not lines:
            raise ValidationError("أضف حساباً واحداً على الأقل.")
        opening = self.company.require_account("default_opening_account")
        for line in lines:
            if line.debit < 0 or line.credit < 0 or (line.debit and line.credit) or not (line.debit or line.credit):
                raise ValidationError(f"الحساب {line.account}: أدخل مبلغاً موجباً في المدين أو الدائن فقط.")
            if line.account_id == opening.pk:
                raise ValidationError("لا تُدخل حساب الأرصدة الافتتاحية نفسه؛ النظام يرحّل الفرق إليه تلقائياً.")
            if line.account.account_type in (AccountType.RECEIVABLE, AccountType.PAYABLE):
                raise ValidationError(f"رصيد {line.account} يُدخل من «الأرصدة الافتتاحية للعملاء والموردين» ليظهر لكل طرف.")
            if line.account.account_type == AccountType.STOCK:
                raise ValidationError(f"رصيد {line.account} يُدخل من «المخزون الافتتاحي» ليطابق كميات الأصناف.")
            account_currency_amount("الرصيد بعملة الحساب", line.account, line.account_currency_amount,
                                    line.debit, line.credit)

    def on_submit(self):
        entries = [{"account": line.account, "debit": line.debit, "credit": line.credit,
                    "remarks": line.description or "رصيد افتتاحي",
                    "account_amount": account_currency_amount("الرصيد بعملة الحساب", line.account,
                                                              line.account_currency_amount, line.debit, line.credit)}
                   for line in self.lines.select_related("account")]
        difference = sum(flt(line.debit) - flt(line.credit) for line in self.lines.all())
        opening = self.company.require_account("default_opening_account")
        entries.append({"account": opening, "debit": -difference if difference < 0 else 0,
                        "credit": difference if difference > 0 else 0, "remarks": "مقابل الأرصدة الافتتاحية"})
        make_gl_entries(self, entries, remarks="رصيد افتتاحي")

    def on_cancel(self):
        cancel_gl_entries(self)


class OpeningAccountLine(models.Model):
    parent = models.ForeignKey(OpeningAccountBalance, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, limit_choices_to={"is_group": False, "disabled": False}, verbose_name="الحساب"
    )
    debit = models.DecimalField("مدين", max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField("دائن", max_digits=18, decimal_places=2, default=0)
    description = models.CharField("البيان", max_length=255, blank=True)
    account_currency_amount = models.DecimalField(
        "الرصيد بعملة الحساب", max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="لحسابات البنك بعملة أجنبية فقط: الرصيد بالدولار مثلاً، والمدين/الدائن قيمته بعملة الشركة")

    class Meta:
        verbose_name = "سطر رصيد افتتاحي"
        verbose_name_plural = "الحسابات"
        ordering = ["id"]


class OpeningPartyBalance(SubmittableDocument):
    """أرصدة العملاء والموردين الافتتاحية، تُرحّل على حساب الذمم لكل طرف مقابل حساب الأرصدة الافتتاحية."""

    naming_prefix = "OPP"

    party_type = models.CharField("نوع الطرف", max_length=10, choices=PartyType.choices, default=PartyType.CUSTOMER)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "أرصدة افتتاحية للعملاء والموردين"
        verbose_name_plural = "الأرصدة الافتتاحية للعملاء والموردين"

    def validate_for_submit(self):
        lines = list(self.lines.select_related("customer", "supplier"))
        if not lines:
            raise ValidationError("أضف طرفاً واحداً على الأقل.")
        for index, line in enumerate(lines, start=1):
            party = line.customer if self.party_type == PartyType.CUSTOMER else line.supplier
            other = line.supplier if self.party_type == PartyType.CUSTOMER else line.customer
            if party is None or other is not None:
                raise ValidationError(f"السطر {index}: حدد {self.get_party_type_display()} فقط.")
            if not line.amount:
                raise ValidationError(f"السطر {index}: أدخل الرصيد.")

    def on_submit(self):
        opening = self.company.require_account("default_opening_account")
        entries = []
        if self.party_type == PartyType.CUSTOMER:
            receivable = self.company.require_account("default_receivable_account")
            for line in self.lines.select_related("customer"):
                # الرصيد الموجب = مستحق لنا على العميل، والسالب = دفعة مقدمة منه
                entries.append({"account": receivable, "debit": line.amount, "customer": line.customer,
                                "remarks": line.reference or "رصيد افتتاحي"})
                entries.append({"account": opening, "credit": line.amount, "remarks": "مقابل أرصدة العملاء الافتتاحية"})
        else:
            payable = self.company.require_account("default_payable_account")
            for line in self.lines.select_related("supplier"):
                entries.append({"account": payable, "credit": line.amount, "supplier": line.supplier,
                                "remarks": line.reference or "رصيد افتتاحي"})
                entries.append({"account": opening, "debit": line.amount, "remarks": "مقابل أرصدة الموردين الافتتاحية"})
        make_gl_entries(self, entries, remarks="رصيد افتتاحي")

    def on_cancel(self):
        cancel_gl_entries(self)


class OpeningPartyLine(models.Model):
    parent = models.ForeignKey(OpeningPartyBalance, on_delete=models.CASCADE, related_name="lines")
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    amount = models.DecimalField("الرصيد المستحق", max_digits=18, decimal_places=2,
                                 help_text="للعميل: ما عليه لنا. للمورد: ما لنا عليه دفعه. القيمة السالبة تعني رصيداً عكسياً.")
    reference = models.CharField("رقم الفاتورة / المرجع القديم", max_length=100, blank=True)

    class Meta:
        verbose_name = "سطر رصيد طرف"
        verbose_name_plural = "الأطراف"
        ordering = ["id"]


class PeriodClosingVoucher(SubmittableDocument):
    """قيد إقفال السنة المالية: تصفير الإيرادات والمصروفات إلى الأرباح المبقاة ثم قفل السنة."""

    naming_prefix = "PCV"

    fiscal_year = models.ForeignKey("core.FiscalYear", on_delete=models.PROTECT, verbose_name="السنة المالية")
    closing_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name="حساب الإقفال (الأرباح المبقاة)",
        limit_choices_to={"is_group": False, "root_type": RootType.EQUITY},
        help_text="يُترك فارغاً لاستخدام حساب الأرباح المبقاة في إعدادات الشركة",
    )
    net_profit = models.DecimalField("صافي الربح / (الخسارة)", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "قيد إقفال سنة مالية"
        verbose_name_plural = "قيود إقفال السنوات المالية"

    def save(self, *args, **kwargs):
        if self.fiscal_year_id:
            self.posting_date = self.fiscal_year.end_date
        super().save(*args, **kwargs)

    def _balances(self):
        rows = (
            GLEntry.objects.filter(
                company=self.company, is_cancelled=False, account__root_type__in=[RootType.INCOME, RootType.EXPENSE],
                posting_date__gte=self.fiscal_year.start_date, posting_date__lte=self.fiscal_year.end_date,
            ).values("account", "cost_center").annotate(d=models.Sum("debit"), c=models.Sum("credit"))
        )
        return {(row["account"], row["cost_center"]): flt(row["d"]) - flt(row["c"]) for row in rows}

    def validate_for_submit(self):
        if self.fiscal_year.company_id != self.company_id:
            raise ValidationError("السنة المالية لا تخص هذه الشركة.")
        others = PeriodClosingVoucher.objects.filter(fiscal_year=self.fiscal_year, docstatus=1).exclude(pk=self.pk)
        if others.exists():
            raise ValidationError(f"السنة المالية {self.fiscal_year} مقفلة بالفعل بالقيد {others.first()}.")
        if self.closing_account is None:
            self.closing_account = self.company.require_account("default_retained_earnings_account")
            self.save(update_fields=["closing_account"])

    def on_submit(self):
        balances = self._balances()
        accounts = Account.objects.in_bulk([key[0] for key in balances])
        centers = CostCenter.objects.in_bulk([key[1] for key in balances if key[1]])
        entries = []
        for (account_id, center_id), amount in balances.items():
            if amount:
                # عكس رصيد كل حساب إيراد/مصروف (لكل مركز تكلفة) ليصبح صفراً
                entries.append({"account": accounts[account_id], "debit": -amount if amount < 0 else 0,
                                "credit": amount if amount > 0 else 0, "cost_center": centers.get(center_id),
                                "remarks": f"إقفال {self.fiscal_year}"})
        self.net_profit = -sum(balances.values(), flt(0))
        if entries:
            entries.append({"account": self.closing_account,
                            "debit": -self.net_profit if self.net_profit < 0 else 0,
                            "credit": self.net_profit if self.net_profit > 0 else 0,
                            "remarks": f"صافي نتيجة {self.fiscal_year}"})
            make_gl_entries(self, entries, remarks=f"قيد إقفال السنة المالية {self.fiscal_year}")
        self.save(update_fields=["net_profit"])
        self.fiscal_year.is_closed = True
        self.fiscal_year.save(update_fields=["is_closed"])

    def cancel(self):
        # إلغاء الإقفال يعيد فتح السنة أولاً حتى يُسمح بعكس القيد
        if self.is_submitted:
            self.fiscal_year.is_closed = False
            self.fiscal_year.save(update_fields=["is_closed"])
        super().cancel()

    def on_cancel(self):
        cancel_gl_entries(self)


class BankTransaction(TenantModel):
    """سطر من كشف حساب البنك، يُطابق مع قيد في الدفاتر عند التسوية البنكية."""

    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, verbose_name="حساب البنك",
        limit_choices_to={"is_group": False, "account_type__in": [AccountType.BANK, AccountType.CASH]},
    )
    date = models.DateField("التاريخ")
    description = models.CharField("البيان", max_length=255, blank=True)
    reference = models.CharField("المرجع", max_length=100, blank=True)
    deposit = models.DecimalField("إيداع (وارد)", max_digits=18, decimal_places=2, default=0)
    withdrawal = models.DecimalField("سحب (صادر)", max_digits=18, decimal_places=2, default=0)
    matched_entry = models.OneToOneField(GLEntry, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
                                         related_name="bank_transaction", verbose_name="القيد المطابق")

    class Meta:
        verbose_name = "حركة كشف بنك"
        verbose_name_plural = "حركات كشف البنك"
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.date:%Y-%m-%d} {self.description or self.reference} {self.amount}"

    @property
    def amount(self):
        return flt(self.deposit) - flt(self.withdrawal)

    def clean(self):
        if (self.deposit and self.withdrawal) or not (self.deposit or self.withdrawal):
            raise ValidationError("أدخل مبلغ الإيداع أو السحب فقط.")
        if self.deposit < 0 or self.withdrawal < 0:
            raise ValidationError("المبالغ يجب أن تكون موجبة.")


def foreign_currency_balances(company, to_date, account_ids=None):
    """أرصدة الحسابات بعملة أجنبية حتى تاريخ: {معرّف الحساب: (الرصيد بعملة الحساب، الرصيد الدفتري بعملة الشركة)}."""
    qs = GLEntry.all_objects.filter(company=company, is_cancelled=False, posting_date__lte=to_date,
                                    account__account_currency__isnull=False)
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)
    rows = qs.values("account").annotate(d=models.Sum("debit"), c=models.Sum("credit"),
                                         fd=models.Sum("debit_in_account_currency"),
                                         fc=models.Sum("credit_in_account_currency"))
    return {row["account"]: (flt(row["fd"]) - flt(row["fc"]), flt(row["d"]) - flt(row["c"])) for row in rows}


class ExchangeRevaluation(SubmittableDocument):
    """إعادة تقييم أرصدة الحسابات بعملة أجنبية بسعر الصرف في نهاية الفترة.

    رصيد الحساب بعملته لا يتغير؛ تتغير قيمته بعملة الشركة فقط، والفرق ربح أو خسارة صرف غير محققة.
    """

    naming_prefix = "EXR"

    total_difference = models.DecimalField("صافي ربح / (خسارة) إعادة التقييم", max_digits=18, decimal_places=2,
                                           default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "إعادة تقييم العملات الأجنبية"
        verbose_name_plural = "إعادة تقييم العملات الأجنبية"

    def fetch_accounts(self):
        """يجلب كل حسابات البنك والصندوق الأجنبية التي لها رصيد حتى تاريخ المستند."""
        balances = foreign_currency_balances(self.company, self.posting_date)
        accounts = Account.objects.filter(pk__in=[pk for pk, (amount, base) in balances.items() if amount or base])
        ExchangeRevaluationLine.objects.bulk_create([
            ExchangeRevaluationLine(parent=self, account=account) for account in accounts.order_by("number", "name")
        ])

    def calculate_totals(self):
        from core.models import ExchangeRate

        if not self.pk:
            return
        if self.is_draft and not self.lines.exists():
            self.fetch_accounts()
        lines = list(self.lines.select_related("account__account_currency"))
        balances = foreign_currency_balances(self.company, self.posting_date, [line.account_id for line in lines])
        total = flt(0)
        for line in lines:
            if self.is_draft:
                line.balance_in_account_currency, line.book_balance = balances.get(line.account_id, (flt(0), flt(0)))
                if not line.new_rate and line.account.account_currency_id:
                    line.new_rate = ExchangeRate.lookup(self.company, line.account.account_currency, self.posting_date)
                line.revalued_balance = flt(line.balance_in_account_currency * line.new_rate) if line.new_rate else 0
                line.difference = flt(line.revalued_balance - line.book_balance) if line.new_rate else 0
                line.save(update_fields=["balance_in_account_currency", "book_balance", "new_rate",
                                         "revalued_balance", "difference"])
            total += line.difference
        self.total_difference = flt(total)

    def validate_for_submit(self):
        lines = list(self.lines.select_related("account__account_currency"))
        if not lines:
            raise ValidationError("لا توجد حسابات بعملة أجنبية لها رصيد حتى هذا التاريخ.")
        seen = set()
        for line in lines:
            if line.account_id in seen:
                raise ValidationError(f"الحساب {line.account} مكرر.")
            seen.add(line.account_id)
            if not line.account.is_foreign_currency:
                raise ValidationError(f"الحساب {line.account} ليس بعملة أجنبية.")
            if not line.new_rate or line.new_rate <= 0:
                raise ValidationError(f"أدخل سعر صرف {line.account.account_currency.code} للحساب {line.account}، "
                                      f"أو أضفه في «الإعدادات ← أسعار الصرف».")
        if not any(line.difference for line in lines):
            raise ValidationError("لا توجد فروق: الأرصدة مقيّمة بالفعل بهذه الأسعار.")

    def on_submit(self):
        fx_account = self.company.require_account("default_exchange_account")
        entries = []
        for line in self.lines.select_related("account__account_currency"):
            if not line.difference:
                continue
            label = f"إعادة تقييم {line.account.account_currency.code} بسعر {line.new_rate.normalize():f}"
            gain = line.difference
            entries.append({"account": line.account, "debit": gain if gain > 0 else 0, "credit": -gain if gain < 0 else 0,
                            "account_amount": 0, "remarks": label})
            entries.append({"account": fx_account, "credit": gain if gain > 0 else 0, "debit": -gain if gain < 0 else 0,
                            "remarks": f"{label} — {line.account}"})
        make_gl_entries(self, entries, remarks="إعادة تقييم أرصدة العملات الأجنبية")

    def on_cancel(self):
        cancel_gl_entries(self)


class ExchangeRevaluationLine(models.Model):
    parent = models.ForeignKey(ExchangeRevaluation, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, verbose_name="الحساب",
        limit_choices_to={"is_group": False, "account_currency__isnull": False},
    )
    balance_in_account_currency = models.DecimalField("الرصيد بعملة الحساب", max_digits=18, decimal_places=2,
                                                      default=0, editable=False)
    book_balance = models.DecimalField("القيمة الدفترية", max_digits=18, decimal_places=2, default=0, editable=False)
    new_rate = models.DecimalField("سعر الصرف الجديد", max_digits=18, decimal_places=6, null=True, blank=True,
                                   help_text="يُترك فارغاً لاستخدام آخر سعر في جدول أسعار الصرف")
    revalued_balance = models.DecimalField("القيمة بعد التقييم", max_digits=18, decimal_places=2, default=0,
                                           editable=False)
    difference = models.DecimalField("الفرق (ربح + / خسارة −)", max_digits=18, decimal_places=2, default=0,
                                     editable=False)

    class Meta:
        verbose_name = "سطر إعادة تقييم"
        verbose_name_plural = "الحسابات"
        ordering = ["id"]


class BudgetAction(models.TextChoices):
    NONE = "none", "متابعة فقط"
    WARN = "warn", "تنبيه المدير عند التجاوز"
    STOP = "stop", "منع الترحيل عند التجاوز"


class Budget(SubmittableDocument):
    """موازنة سنوية لحسابات المصروفات (للشركة كلها أو لمركز تكلفة)، تُقارن بالفعلي وتُراقب عند الترحيل."""

    naming_prefix = "BUD"
    posts_to_ledger = False
    posting_note = "الموازنة لا تُنشئ قيوداً؛ بعد اعتمادها تُقارن بالفعلي ويُطبق إجراء التجاوز عند ترحيل المصروفات."

    fiscal_year = models.ForeignKey("core.FiscalYear", models.PROTECT, verbose_name="السنة المالية")
    cost_center = models.ForeignKey(CostCenter, models.PROTECT, null=True, blank=True, verbose_name="مركز التكلفة / الفرع",
                                    help_text="فارغ = موازنة الشركة كلها. المركز الرئيسي يشمل فروعه")
    action = models.CharField("عند تجاوز الموازنة", max_length=10, choices=BudgetAction.choices,
                              default=BudgetAction.WARN)
    total_amount = models.DecimalField("إجمالي الموازنة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "موازنة تقديرية"
        verbose_name_plural = "الموازنات التقديرية"

    def __str__(self):
        return f"{self.name} - {self.fiscal_year}" + (f" - {self.cost_center}" if self.cost_center_id else "")

    def save(self, *args, **kwargs):
        if self.fiscal_year_id and self.is_draft:
            self.posting_date = self.fiscal_year.start_date
        super().save(*args, **kwargs)

    def calculate_totals(self):
        if self.pk:
            self.total_amount = flt(sum((flt(line.amount) for line in self.lines.all()), flt(0)))

    def validate_for_submit(self):
        lines = list(self.lines.select_related("account"))
        if not lines:
            raise ValidationError("أضف حساباً واحداً على الأقل.")
        seen = set()
        for line in lines:
            if line.account_id in seen:
                raise ValidationError(f"الحساب {line.account} مكرر في الموازنة.")
            seen.add(line.account_id)
            if line.account.root_type != RootType.EXPENSE or line.account.is_group:
                raise ValidationError(f"الحساب {line.account}: الموازنة لحسابات المصروفات الفرعية فقط.")
            if flt(line.amount) <= 0:
                raise ValidationError(f"الحساب {line.account}: أدخل مبلغ الموازنة.")
        clash = Budget.objects.filter(fiscal_year=self.fiscal_year, cost_center=self.cost_center,
                                      docstatus=1).exclude(pk=self.pk).first()
        if clash:
            raise ValidationError(f"توجد موازنة معتمدة لنفس السنة والمركز ({clash.name}). ألغها أولاً لاعتماد موازنة معدلة.")

    def center_ids(self):
        return self.cost_center.descendant_ids() if self.cost_center_id else None

    def actuals(self, to_date=None, exclude_voucher=None):
        """الفعلي لكل حساب في الموازنة من بداية السنة حتى التاريخ (صافي المدين)."""
        qs = GLEntry.all_objects.filter(company=self.company, is_cancelled=False,
                                        account_id__in=self.lines.values("account_id"),
                                        posting_date__gte=self.fiscal_year.start_date,
                                        posting_date__lte=min(to_date or self.fiscal_year.end_date, self.fiscal_year.end_date))
        if self.cost_center_id:
            qs = qs.filter(cost_center_id__in=self.center_ids())
        if exclude_voucher:
            qs = qs.exclude(voucher_type=exclude_voucher)
        rows = qs.values("account_id").annotate(d=models.Sum("debit"), c=models.Sum("credit"))
        return {row["account_id"]: flt(row["d"]) - flt(row["c"]) for row in rows}


class BudgetLine(models.Model):
    parent = models.ForeignKey(Budget, models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, models.PROTECT, verbose_name="الحساب",
                                limit_choices_to={"is_group": False, "root_type": RootType.EXPENSE})
    amount = models.DecimalField("الموازنة السنوية", max_digits=18, decimal_places=2)
    notes = models.CharField("ملاحظة", max_length=255, blank=True)

    class Meta:
        verbose_name = "سطر موازنة"
        verbose_name_plural = "الحسابات"
        ordering = ["id"]


class ChequeStatus(models.TextChoices):
    PENDING = "pending", "تحت التحصيل / لم يُصرف"
    CLEARED = "cleared", "صُرف"
    BOUNCED = "bounced", "مرتد"
    CANCELLED = "cancelled", "ملغى"


class Cheque(TenantModel):
    """شيك وارد أو صادر من سند قبض/صرف، يُتابع حتى صرفه من البنك أو ارتداده."""

    payment = models.OneToOneField(PaymentEntry, models.CASCADE, related_name="cheque", verbose_name="السند")
    status = models.CharField("الحالة", max_length=10, choices=ChequeStatus.choices, default=ChequeStatus.PENDING)
    status_date = models.DateField("تاريخ الحالة", null=True, blank=True)
    clearance_entry = models.ForeignKey(JournalEntry, models.SET_NULL, null=True, blank=True, related_name="+",
                                        verbose_name="قيد الصرف")
    bounce_entry = models.ForeignKey(JournalEntry, models.SET_NULL, null=True, blank=True, related_name="+",
                                     verbose_name="قيد الارتداد")
    bounce_reason = models.CharField("سبب الارتداد", max_length=255, blank=True)

    class Meta:
        verbose_name = "شيك"
        verbose_name_plural = "الشيكات"
        ordering = ["payment__cheque_date", "id"]

    def __str__(self):
        return f"شيك {self.cheque_no}"

    @property
    def cheque_no(self):
        return self.payment.cheque_no

    @property
    def is_received(self):
        return self.payment.payment_type == PaymentType.RECEIVE
