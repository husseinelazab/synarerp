from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
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
    COGS = "cogs", "تكلفة البضاعة المباعة"
    STOCK_ADJUSTMENT = "stock_adjustment", "تسوية مخزون"
    INCOME = "income_account", "حساب إيراد"
    EXPENSE = "expense_account", "حساب مصروف"
    EQUITY = "equity", "حقوق ملكية"


class Account(TenantModel):
    number = models.CharField("رقم الحساب", max_length=20, blank=True)
    name = models.CharField("اسم الحساب", max_length=150)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children", verbose_name="الحساب الأب"
    )
    is_group = models.BooleanField("حساب رئيسي (مجموعة)", default=False)
    root_type = models.CharField("النوع الجذري", max_length=20, choices=RootType.choices)
    account_type = models.CharField("نوع الحساب", max_length=30, choices=AccountType.choices, blank=True)
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

    def clean(self):
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
    voucher_type = models.CharField("نوع المستند", max_length=50)
    voucher_no = models.CharField("رقم المستند", max_length=40)
    voucher_id = models.PositiveBigIntegerField("معرّف المستند")
    remarks = models.CharField("البيان", max_length=255, blank=True)
    is_cancelled = models.BooleanField("ملغى", default=False)
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
                "remarks": line.description or self.remarks,
            }
            for line in self.accounts.select_related("account", "customer", "supplier")
        ])

    def on_cancel(self):
        cancel_gl_entries(self)


class JournalEntryAccount(models.Model):
    parent = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="accounts")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, limit_choices_to={"is_group": False, "disabled": False}, verbose_name="الحساب"
    )
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    debit = models.DecimalField("مدين", max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField("دائن", max_digits=18, decimal_places=2, default=0)
    description = models.CharField("البيان", max_length=255, blank=True)

    class Meta:
        verbose_name = "سطر قيد"
        verbose_name_plural = "سطور القيد"

    def validate_line(self):
        if self.debit and self.credit:
            raise ValidationError(f"الحساب {self.account}: لا يمكن أن يكون السطر مديناً ودائناً معاً.")
        if self.debit < 0 or self.credit < 0:
            raise ValidationError(f"الحساب {self.account}: لا يُسمح بالقيم السالبة.")
        if self.account.account_type == AccountType.RECEIVABLE and not self.customer_id:
            raise ValidationError(f"الحساب {self.account} حساب عملاء ويتطلب تحديد العميل.")
        if self.account.account_type == AccountType.PAYABLE and not self.supplier_id:
            raise ValidationError(f"الحساب {self.account} حساب موردين ويتطلب تحديد المورد.")


class PaymentType(models.TextChoices):
    RECEIVE = "receive", "سند قبض"
    PAY = "pay", "سند صرف"


class PaymentEntry(SubmittableDocument):
    naming_prefix = "PAY"

    payment_type = models.CharField("نوع السند", max_length=10, choices=PaymentType.choices, default=PaymentType.RECEIVE)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, verbose_name="حساب الصندوق / البنك",
        limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH, AccountType.BANK]},
    )
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    sales_invoice = models.ForeignKey(
        "selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="payments", verbose_name="فاتورة المبيعات"
    )
    purchase_invoice = models.ForeignKey(
        "buying.PurchaseInvoice", models.PROTECT, null=True, blank=True, related_name="payments", verbose_name="فاتورة المشتريات"
    )
    reference_no = models.CharField("رقم المرجع (شيك / حوالة)", max_length=50, blank=True)

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

    def validate_for_submit(self):
        if flt(self.amount) <= 0:
            raise ValidationError("المبلغ يجب أن يكون أكبر من صفر.")
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
        if self.payment_type == PaymentType.RECEIVE:
            entries = [
                {"account": self.account, "debit": amount},
                {"account": party_account, "credit": amount, **party},
            ]
        else:
            entries = [
                {"account": party_account, "debit": amount, **party},
                {"account": self.account, "credit": amount},
            ]
        make_gl_entries(self, entries, remarks=self.remarks or self.get_payment_type_display())
        self._update_invoice_outstanding(-amount * self.direction)

    def on_cancel(self):
        cancel_gl_entries(self)
        self._update_invoice_outstanding(flt(self.amount) * self.direction)

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

    def on_submit(self):
        entries = [{"account": line.account, "debit": line.debit, "credit": line.credit,
                    "remarks": line.description or "رصيد افتتاحي"} for line in self.lines.select_related("account")]
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

    class Meta:
        verbose_name = "سطر رصيد افتتاحي"
        verbose_name_plural = "الحسابات"


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
            ).values("account").annotate(d=models.Sum("debit"), c=models.Sum("credit"))
        )
        return {row["account"]: flt(row["d"]) - flt(row["c"]) for row in rows}

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
        accounts = Account.objects.in_bulk(list(balances))
        entries = []
        for account_id, amount in balances.items():
            if amount:
                # عكس رصيد كل حساب إيراد/مصروف ليصبح صفراً
                entries.append({"account": accounts[account_id], "debit": -amount if amount < 0 else 0,
                                "credit": amount if amount > 0 else 0, "remarks": f"إقفال {self.fiscal_year}"})
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
