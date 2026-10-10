from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.models import Company, SubmittableDocument
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


class Account(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="accounts", verbose_name="الشركة")
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
        unique_together = [("company", "name")]

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


class GLEntry(models.Model):
    """قيد في دفتر الأستاذ العام - يُنشأ تلقائياً عند اعتماد المستندات."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, verbose_name="الشركة")
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
        indexes = [models.Index(fields=["voucher_type", "voucher_no"]), models.Index(fields=["account", "posting_date"])]

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
        return self.customer if self.payment_type == PaymentType.RECEIVE else self.supplier

    @property
    def invoice(self):
        return self.sales_invoice if self.payment_type == PaymentType.RECEIVE else self.purchase_invoice

    def validate_for_submit(self):
        if flt(self.amount) <= 0:
            raise ValidationError("المبلغ يجب أن يكون أكبر من صفر.")
        if self.payment_type == PaymentType.RECEIVE:
            if not self.customer_id:
                raise ValidationError("سند القبض يتطلب تحديد العميل.")
            invoice, party_field = self.sales_invoice, "customer_id"
        else:
            if not self.supplier_id:
                raise ValidationError("سند الصرف يتطلب تحديد المورد.")
            invoice, party_field = self.purchase_invoice, "supplier_id"
        if invoice:
            if not invoice.is_submitted:
                raise ValidationError("الفاتورة المرجعية غير معتمدة.")
            if getattr(invoice, party_field) != getattr(self, party_field):
                raise ValidationError("الفاتورة المرجعية لا تخص الطرف المحدد.")
            if flt(self.amount) > flt(invoice.outstanding_amount):
                raise ValidationError(f"المبلغ أكبر من المتبقي على الفاتورة ({invoice.outstanding_amount}).")

    def on_submit(self):
        amount = flt(self.amount)
        if self.payment_type == PaymentType.RECEIVE:
            receivable = self.company.require_account("default_receivable_account")
            entries = [
                {"account": self.account, "debit": amount},
                {"account": receivable, "credit": amount, "customer": self.customer},
            ]
        else:
            payable = self.company.require_account("default_payable_account")
            entries = [
                {"account": payable, "debit": amount, "supplier": self.supplier},
                {"account": self.account, "credit": amount},
            ]
        make_gl_entries(self, entries, remarks=self.remarks or self.get_payment_type_display())
        self._update_invoice_outstanding(-amount)

    def on_cancel(self):
        cancel_gl_entries(self)
        self._update_invoice_outstanding(flt(self.amount))

    def _update_invoice_outstanding(self, delta):
        invoice = self.invoice
        if invoice:
            invoice.outstanding_amount = flt(invoice.outstanding_amount + delta)
            invoice.save(update_fields=["outstanding_amount"])
