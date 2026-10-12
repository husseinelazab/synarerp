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
    CHEQUES_RECEIVABLE = "cheques_receivable", "أوراق قبض (شيكات تحت التحصيل)"
    CHEQUES_PAYABLE = "cheques_payable", "أوراق دفع (شيكات صادرة)"


class CashFlowCategory(models.TextChoices):
    OPERATING = "operating", "أنشطة تشغيلية"
    INVESTING = "investing", "أنشطة استثمارية"
    FINANCING = "financing", "أنشطة تمويلية"


class Account(TenantModel):
    number = models.CharField("رقم الحساب", max_length=20, blank=True)
    name = models.CharField("اسم الحساب", max_length=150)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children", verbose_name="الحساب الأب"
    )
    is_group = models.BooleanField("حساب رئيسي (مجموعة)", default=False)
    root_type = models.CharField("النوع الجذري", max_length=20, choices=RootType.choices)
    account_type = models.CharField("نوع الحساب", max_length=30, choices=AccountType.choices, blank=True)
    cash_flow_category = models.CharField(
        "تصنيف التدفق النقدي", max_length=10, choices=CashFlowCategory.choices, blank=True,
        help_text="فارغ = تلقائي: الأصول الثابتة استثمارية، وحقوق الملكية والقروض تمويلية، والباقي تشغيلي")
    zakat_treatment = models.CharField(
        "المعاملة الزكوية", max_length=10, blank=True,
        choices=[("add", "يُضاف للوعاء"), ("deduct", "يُحسم من الوعاء"), ("ignore", "لا يدخل الوعاء")],
        help_text="فارغ = تلقائي: حقوق الملكية والخصوم طويلة الأجل تُضاف، والأصول الثابتة تُحسم")
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

    @property
    def effective_cash_flow_category(self):
        if self.cash_flow_category:
            return self.cash_flow_category
        if self.account_type in (AccountType.FIXED_ASSET, AccountType.ACCUMULATED_DEPRECIATION):
            return CashFlowCategory.INVESTING
        if self.root_type == RootType.EQUITY:
            return CashFlowCategory.FINANCING
        return CashFlowCategory.OPERATING


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
    project = models.ForeignKey("projects.Project", models.PROTECT, null=True, blank=True, related_name="+",
                                verbose_name="المشروع")
    voucher_type = models.CharField("نوع المستند", max_length=50)
    voucher_no = models.CharField("رقم المستند", max_length=40)
    voucher_id = models.PositiveBigIntegerField("معرّف المستند")
    remarks = models.CharField("البيان", max_length=255, blank=True)
    is_cancelled = models.BooleanField("ملغى", default=False)
    clearance_date = models.DateField("تاريخ الظهور في كشف البنك", null=True, blank=True)
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
                "cost_center": line.cost_center,
                "project": line.project,
                "remarks": line.description or self.remarks,
            }
            for line in self.accounts.select_related("account", "customer", "supplier")
        ])

    def before_cancel(self):
        from assets.services import check_journal_cancel

        check_journal_cancel(self)

    def on_cancel(self):
        from assets.services import on_journal_cancel

        cancel_gl_entries(self)
        on_journal_cancel(self)


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
    project = models.ForeignKey("projects.Project", models.PROTECT, null=True, blank=True, related_name="+",
                                verbose_name="المشروع")
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


class PaymentTermsTemplate(TenantModel):
    """شروط الدفع: دفعة واحدة بعد مدة، أو أقساط (مقدم + دفعات شهرية مثلاً)."""

    name = models.CharField("اسم الشروط", max_length=100, help_text="مثل: آجل 30 يوماً، أو 30% مقدم والباقي على 3 أقساط")
    disabled = models.BooleanField("موقوفة", default=False)

    class Meta:
        verbose_name = "شروط دفع"
        verbose_name_plural = "شروط الدفع والأقساط"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="paymenttermstemplate")]

    def __str__(self):
        return self.name

    def validate_lines(self):
        lines = list(self.lines.all())
        if not lines:
            raise ValidationError(f"شروط الدفع «{self}» بلا أقساط.")
        total = sum((flt(line.invoice_portion) for line in lines), flt(0))
        if total != 100:
            raise ValidationError(f"مجموع نسب أقساط «{self}» يجب أن يكون 100% (الحالي {total}%).")

    def schedule(self, posting_date, grand_total):
        """[(تاريخ الاستحقاق، المبلغ، البيان)] — فرق التقريب على القسط الأخير."""
        import datetime

        self.validate_lines()
        lines = list(self.lines.all())
        rows, allocated = [], flt(0)
        for index, line in enumerate(lines):
            amount = flt(grand_total) - allocated if index == len(lines) - 1 else \
                flt(flt(grand_total) * flt(line.invoice_portion) / 100)
            allocated += amount
            rows.append((posting_date + datetime.timedelta(days=line.credit_days), amount,
                         line.description or f"القسط {index + 1}"))
        return rows


class PaymentTermLine(models.Model):
    parent = models.ForeignKey(PaymentTermsTemplate, models.CASCADE, related_name="lines")
    description = models.CharField("البيان", max_length=100, blank=True, help_text="مثل: دفعة مقدمة، القسط الأول")
    invoice_portion = models.DecimalField("النسبة من الفاتورة %", max_digits=6, decimal_places=2)
    credit_days = models.PositiveIntegerField("يستحق بعد (يوم)", default=0, help_text="من تاريخ الفاتورة. صفر = عند الفاتورة")

    class Meta:
        verbose_name = "قسط"
        verbose_name_plural = "الأقساط"
        ordering = ["credit_days", "id"]


class PaymentSchedule(models.Model):
    """جدول أقساط فاتورة: يُبنى من شروط الدفع، والمسدد يُوزع على الأقساط الأقدم أولاً."""

    sales_invoice = models.ForeignKey("selling.SalesInvoice", models.CASCADE, null=True, blank=True,
                                      related_name="schedule")
    purchase_invoice = models.ForeignKey("buying.PurchaseInvoice", models.CASCADE, null=True, blank=True,
                                         related_name="schedule")
    due_date = models.DateField("تاريخ الاستحقاق")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    description = models.CharField("البيان", max_length=100, blank=True)

    class Meta:
        verbose_name = "قسط فاتورة"
        verbose_name_plural = "جدول الأقساط"
        ordering = ["due_date", "id"]


class PaymentType(models.TextChoices):
    RECEIVE = "receive", "سند قبض"
    PAY = "pay", "سند صرف"


class PaymentEntry(CurrencyMixin, SubmittableDocument):
    naming_prefix = "PAY"

    payment_type = models.CharField("نوع السند", max_length=10, choices=PaymentType.choices, default=PaymentType.RECEIVE)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, verbose_name="حساب الصندوق / البنك",
        limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH, AccountType.BANK,
                                                                   AccountType.CHEQUES_RECEIVABLE,
                                                                   AccountType.CHEQUES_PAYABLE]},
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
        "selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="payments", verbose_name="فاتورة المبيعات",
        help_text="لسداد فاتورة واحدة. لتوزيع السند على عدة فواتير استخدم جدول «التخصيص على الفواتير»",
    )
    purchase_invoice = models.ForeignKey(
        "buying.PurchaseInvoice", models.PROTECT, null=True, blank=True, related_name="payments", verbose_name="فاتورة المشتريات"
    )
    reference_no = models.CharField("رقم المرجع (شيك / حوالة)", max_length=50, blank=True)
    withholding_rate = models.DecimalField("ضريبة الاستقطاع %", max_digits=5, decimal_places=2, default=0,
                                           help_text="للدفع لمورد غير مقيم: يُخصم من المدفوع ويُستحق للهيئة. "
                                                     "المبلغ أعلاه = الإجمالي المسدد للمورد قبل الاستقطاع")
    withholding_amount = models.DecimalField("ضريبة الاستقطاع", max_digits=18, decimal_places=2, default=0,
                                             editable=False)
    allocated_amount = models.DecimalField("المخصص على الفواتير", max_digits=18, decimal_places=2, default=0,
                                           editable=False)
    unallocated_amount = models.DecimalField("غير مخصص (دفعة مقدمة)", max_digits=18, decimal_places=2, default=0,
                                             editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "سند قبض / صرف"
        verbose_name_plural = "سندات القبض والصرف"

    @property
    def party(self):
        return self.customer or self.supplier

    @property
    def party_field(self):
        return "customer" if self.customer_id else "supplier"

    @property
    def invoice(self):
        return self.sales_invoice if self.customer_id else self.purchase_invoice

    @property
    def direction(self):
        """+1 للاتجاه المعتاد (قبض من عميل / صرف لمورد)، و-1 للرد (صرف لعميل / قبض من مورد)."""
        normal = (self.payment_type == PaymentType.RECEIVE) == bool(self.customer_id)
        return 1 if normal else -1

    @property
    def party_account(self):
        field = "default_receivable_account" if self.customer_id else "default_payable_account"
        return self.company.require_account(field)

    def allocations(self):
        """[(الفاتورة، المبلغ المخصص)] من جدول التخصيص، أو من حقل الفاتورة الواحدة في رأس السند."""
        rows = [(ref.invoice, flt(ref.allocated_amount)) for ref in self.references.all() if ref.invoice is not None]
        if not rows and self.invoice is not None:
            rows = [(self.invoice, flt(self.amount))]
        return rows

    def save(self, *args, **kwargs):
        self.ensure_currency()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        self.ensure_currency()
        self.base_amount = self.to_base(self.amount)
        if self.is_draft and self.supplier_id and self.payment_type == PaymentType.PAY and not flt(self.withholding_rate):
            self.withholding_rate = self.supplier.withholding_rate
        if not (self.supplier_id and self.payment_type == PaymentType.PAY):
            self.withholding_rate = 0
        self.withholding_amount = flt(flt(self.amount) * flt(self.withholding_rate) / 100)
        if self.pk:
            self.allocated_amount = sum((amount for _inv, amount in self.allocations()), flt(0))
            self.unallocated_amount = flt(flt(self.amount) - self.allocated_amount)

    def validate_for_submit(self):
        self.validate_currency()
        if flt(self.amount) <= 0:
            raise ValidationError("المبلغ يجب أن يكون أكبر من صفر.")
        if bool(self.customer_id) == bool(self.supplier_id):
            raise ValidationError("حدد عميلاً أو مورداً واحداً فقط.")
        if self.customer_id and self.purchase_invoice_id or self.supplier_id and self.sales_invoice_id:
            raise ValidationError("الفاتورة المرجعية لا تتوافق مع نوع الطرف.")
        refs = list(self.references.all())
        if refs and self.invoice is not None:
            raise ValidationError("حدد الفاتورة في رأس السند أو في جدول التخصيص، لا في كليهما.")
        seen = set()
        for ref in refs:
            if ref.invoice is None:
                raise ValidationError("حدد الفاتورة في كل سطر من جدول التخصيص.")
            if self.customer_id and ref.purchase_invoice_id or self.supplier_id and ref.sales_invoice_id:
                raise ValidationError(f"الفاتورة {ref.invoice} لا تتوافق مع نوع الطرف.")
            if ref.invoice.pk in seen:
                raise ValidationError(f"الفاتورة {ref.invoice} مكررة في جدول التخصيص.")
            seen.add(ref.invoice.pk)
            if flt(ref.allocated_amount) <= 0:
                raise ValidationError(f"المبلغ المخصص للفاتورة {ref.invoice} يجب أن يكون أكبر من صفر.")
        total = flt(0)
        for invoice, amount in self.allocations():
            self.validate_invoice(invoice, amount)
            total += amount
        if total > flt(self.amount):
            raise ValidationError(f"مجموع المخصص على الفواتير ({total}) أكبر من مبلغ السند ({self.amount}).")

    def validate_invoice(self, invoice, amount):
        if not invoice.is_submitted:
            raise ValidationError(f"الفاتورة {invoice} غير معتمدة.")
        if getattr(invoice, f"{self.party_field}_id") != getattr(self, f"{self.party_field}_id"):
            raise ValidationError(f"الفاتورة {invoice} لا تخص الطرف المحدد.")
        if invoice.currency_id != self.currency_id:
            raise ValidationError(f"عملة السند يجب أن تطابق عملة الفاتورة {invoice} ({invoice.currency}).")
        available = flt(invoice.outstanding_amount) * self.direction
        if available <= 0:
            raise ValidationError(f"لا يوجد رصيد مستحق على الفاتورة {invoice} في هذا الاتجاه.")
        if amount > available:
            raise ValidationError(f"المبلغ المخصص للفاتورة {invoice} أكبر من المتبقي عليها ({available}).")

    def on_submit(self):
        amount = flt(self.amount)
        party_account = self.party_account
        party = {"customer": self.customer, "supplier": self.supplier}
        allocations = self.allocations()
        # النقدية بسعر صرف السند، والذمة بسعر صرف كل فاتورة نشأت بها (والجزء غير المخصص بسعر السند)؛
        # الفرق ربح أو خسارة صرف
        wht = flt(self.withholding_amount) if self.payment_type == PaymentType.PAY and self.supplier_id else flt(0)
        wht_base = self.to_base(wht)
        cash_base = self.to_base(amount - wht)
        allocated = sum((a for _inv, a in allocations), flt(0))
        party_base = sum((flt(a * inv.exchange_rate) for inv, a in allocations), flt(0)) + self.to_base(amount - allocated)
        if self.payment_type == PaymentType.RECEIVE:
            entries = [
                {"account": self.account, "debit": cash_base, "base": True},
                {"account": party_account, "credit": party_base, "base": True, **party},
            ]
            gain = cash_base - party_base
        else:
            entries = [
                {"account": party_account, "debit": party_base, "base": True, **party},
                {"account": self.account, "credit": cash_base, "base": True},
            ]
            if wht:
                entries.append({"account": withholding_account(self.company), "credit": wht_base, "base": True,
                                "remarks": f"ضريبة استقطاع {self.withholding_rate}% — {self.supplier}"})
            gain = party_base - cash_base - wht_base
        if gain:
            fx_account = self.company.require_account("default_exchange_account")
            entries.append({"account": fx_account, "credit": gain if gain > 0 else 0, "debit": -gain if gain < 0 else 0,
                            "base": True, "remarks": "فرق سعر الصرف"})
        self.exchange_difference = gain
        self.allocated_amount = allocated
        self.unallocated_amount = flt(amount - allocated)
        self.save(update_fields=["exchange_difference", "allocated_amount", "unallocated_amount"])
        make_gl_entries(self, entries, remarks=self.remarks or self.get_payment_type_display())
        for invoice, value in allocations:
            _update_outstanding(invoice, -value * self.direction)

    def before_cancel(self):
        if self.reconciliation_lines.filter(parent__docstatus=1).exists():
            raise ValidationError("خُصص جزء من هذا السند على فواتير بمستند «تسوية الدفعات». ألغِ التسوية أولاً.")

    def on_cancel(self):
        cancel_gl_entries(self)
        for invoice, value in self.allocations():
            _update_outstanding(invoice, value * self.direction)

    # ---------- التوزيع التلقائي ----------

    @property
    def draft_actions(self):
        if self.is_draft and self.party is not None and self.invoice is None:
            return [("allocate_oldest", "توزيع تلقائي على أقدم الفواتير المستحقة")]
        return []

    def open_invoices(self):
        """فواتير الطرف المستحقة في اتجاه السند، الأقدم استحقاقاً أولاً."""
        from buying.models import PurchaseInvoice
        from selling.models import SalesInvoice

        model = SalesInvoice if self.customer_id else PurchaseInvoice
        qs = model.objects.filter(**{self.party_field: self.party}, docstatus=1, currency=self.currency_id)
        qs = qs.filter(outstanding_amount__gt=0) if self.direction > 0 else qs.filter(outstanding_amount__lt=0)
        return sorted(qs, key=lambda inv: (inv.due_date or inv.posting_date, inv.posting_date, inv.pk))

    def allocate_oldest(self, user=None):
        """يملأ جدول التخصيص بأقدم الفواتير حتى ينفد مبلغ السند؛ الباقي يبقى دفعة مقدمة."""
        self.ensure_currency()
        self.save()
        self.references.all().delete()
        remaining = flt(self.amount)
        for invoice in self.open_invoices():
            if remaining <= 0:
                break
            share = min(remaining, abs(flt(invoice.outstanding_amount)))
            self.references.create(**{"sales_invoice" if self.customer_id else "purchase_invoice": invoice},
                                   allocated_amount=share)
            remaining -= share
        if not self.references.exists():
            raise ValidationError("لا توجد فواتير مستحقة لهذا الطرف بنفس عملة السند.")
        self.calculate_totals()
        self.save()


def withholding_account(company):
    """حساب ضريبة الاستقطاع المستحقة (يُنشأ تحت الخصوم المتداولة إن لم يوجد)."""
    account = Account.all_objects.filter(company=company, number="2108", is_group=False).first()
    if account is None:
        parent = (Account.all_objects.filter(company=company, number="21", is_group=True).first() or
                  Account.all_objects.filter(company=company, root_type=RootType.LIABILITY, is_group=True,
                                             parent__isnull=True).first())
        account = Account.all_objects.create(company=company, number="2108", name="ضريبة الاستقطاع المستحقة",
                                             parent=parent, root_type=RootType.LIABILITY, account_type=AccountType.TAX)
    return account


def _update_outstanding(invoice, delta):
    invoice = type(invoice).objects.select_for_update().get(pk=invoice.pk)
    invoice.outstanding_amount = flt(invoice.outstanding_amount + delta)
    invoice.save(update_fields=["outstanding_amount"])


class PaymentReference(models.Model):
    """سطر تخصيص: جزء من مبلغ السند يُسدد فاتورة بعينها."""

    parent = models.ForeignKey(PaymentEntry, on_delete=models.CASCADE, related_name="references")
    sales_invoice = models.ForeignKey(
        "selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="payment_references",
        verbose_name="فاتورة المبيعات", limit_choices_to=models.Q(docstatus=1) & ~models.Q(outstanding_amount=0))
    purchase_invoice = models.ForeignKey(
        "buying.PurchaseInvoice", models.PROTECT, null=True, blank=True, related_name="payment_references",
        verbose_name="فاتورة المشتريات", limit_choices_to=models.Q(docstatus=1) & ~models.Q(outstanding_amount=0))
    allocated_amount = models.DecimalField("المبلغ المخصص", max_digits=18, decimal_places=2, default=0)

    class Meta:
        verbose_name = "تخصيص على فاتورة"
        verbose_name_plural = "التخصيص على الفواتير"

    @property
    def invoice(self):
        return self.sales_invoice or self.purchase_invoice


class PaymentReconciliation(SubmittableDocument):
    """تسوية الدفعات: ربط الدفعات المقدمة والإشعارات الدائنة غير المخصصة بفواتير الطرف المفتوحة.

    لا تغيّر رصيد الطرف (الدفعة مرحّلة أصلاً)؛ تُنقص المتبقي على الفواتير، وترحّل فرق الصرف فقط
    عند اختلاف سعر صرف الدفعة عن سعر صرف الفاتورة.
    """

    naming_prefix = "PREC"

    party_type = models.CharField("نوع الطرف", max_length=10, choices=PartyType.choices, default=PartyType.CUSTOMER)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    total_allocated = models.DecimalField("إجمالي المسوّى", max_digits=18, decimal_places=2, default=0, editable=False)
    exchange_difference = models.DecimalField("فرق سعر الصرف (ربح + / خسارة −)", max_digits=18, decimal_places=2,
                                              default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "تسوية دفعات"
        verbose_name_plural = "تسويات الدفعات"

    @property
    def party(self):
        return self.customer if self.party_type == PartyType.CUSTOMER else self.supplier

    @property
    def party_field(self):
        return "customer" if self.party_type == PartyType.CUSTOMER else "supplier"

    def calculate_totals(self):
        self.total_allocated = sum((flt(line.amount) for line in self.lines.all()), flt(0))

    def validate_for_submit(self):
        party = self.party
        if party is None:
            raise ValidationError(f"حدد {self.get_party_type_display()}.")
        lines = list(self.lines.select_related("payment", "sales_return", "purchase_return", "sales_invoice",
                                               "purchase_invoice"))
        if not lines:
            raise ValidationError("أضف سطر تسوية واحداً على الأقل.")
        used_sources, used_targets = {}, {}
        for index, line in enumerate(lines, start=1):
            source, target = line.source, line.target
            if source is None or target is None:
                raise ValidationError(f"السطر {index}: حدد الدفعة أو الإشعار الدائن، والفاتورة المراد تسويتها.")
            if (line.payment_id and (line.sales_return_id or line.purchase_return_id)) or \
                    (line.sales_invoice_id and line.purchase_invoice_id):
                raise ValidationError(f"السطر {index}: مصدر واحد وفاتورة واحدة فقط لكل سطر.")
            if flt(line.amount) <= 0:
                raise ValidationError(f"السطر {index}: المبلغ يجب أن يكون أكبر من صفر.")
            for doc in (source, target):
                if not doc.is_submitted:
                    raise ValidationError(f"السطر {index}: المستند {doc} غير معتمد.")
                if getattr(doc, f"{self.party_field}_id") != party.pk:
                    raise ValidationError(f"السطر {index}: المستند {doc} لا يخص {party}.")
            if source.currency_id != target.currency_id:
                raise ValidationError(f"السطر {index}: عملة {source} تختلف عن عملة {target}.")
            if line.payment_id:
                if line.payment.direction < 0:
                    raise ValidationError(f"السطر {index}: السند {source} سند رد ولا يُسوّى على فواتير.")
                available = flt(line.payment.unallocated_amount)
            else:
                if not source.is_return:
                    raise ValidationError(f"السطر {index}: {source} ليس إشعاراً دائناً (مرتجعاً).")
                available = -flt(source.outstanding_amount)
            key = (type(source).__name__, source.pk)
            used_sources[key] = used_sources.get(key, flt(0)) + flt(line.amount)
            if used_sources[key] > available:
                raise ValidationError(f"السطر {index}: المتاح للتسوية من {source} هو {available}.")
            if target.is_return:
                raise ValidationError(f"السطر {index}: لا يُسوّى على مرتجع؛ اختر فاتورة.")
            tkey = (type(target).__name__, target.pk)
            used_targets[tkey] = used_targets.get(tkey, flt(0)) + flt(line.amount)
            if used_targets[tkey] > flt(target.outstanding_amount):
                raise ValidationError(f"السطر {index}: المتبقي على الفاتورة {target} هو {target.outstanding_amount}.")

    def _apply(self, sign):
        entries, gain = [], flt(0)
        party_account = self.company.require_account(
            "default_receivable_account" if self.party_type == PartyType.CUSTOMER else "default_payable_account")
        fx = None
        for line in self.lines.select_related("payment", "sales_return", "purchase_return", "sales_invoice",
                                              "purchase_invoice"):
            amount = flt(line.amount) * sign
            source, target = line.source, line.target
            if line.payment_id:
                payment = PaymentEntry.objects.select_for_update().get(pk=line.payment_id)
                payment.allocated_amount = flt(payment.allocated_amount + amount)
                payment.unallocated_amount = flt(payment.unallocated_amount - amount)
                payment.save(update_fields=["allocated_amount", "unallocated_amount"])
            else:
                _update_outstanding(source, amount)
            _update_outstanding(target, -amount)
            # الذمة سُجلت بسعر صرف الفاتورة وخُصمت بسعر صرف المصدر: الفرق يُقفل في حساب فروق الصرف
            diff = flt(flt(line.amount) * (flt(target.exchange_rate or 1, 6) - flt(source.exchange_rate or 1, 6)))
            if diff:
                fx = fx or self.company.require_account("default_exchange_account")
                party = {self.party_field: self.party}
                if self.party_type == PartyType.CUSTOMER:
                    entries += [{"account": party_account, "credit": diff, **party},
                                {"account": fx, "debit": diff, "remarks": "فرق سعر الصرف"}]
                    gain -= diff
                else:
                    entries += [{"account": party_account, "debit": diff, **party},
                                {"account": fx, "credit": diff, "remarks": "فرق سعر الصرف"}]
                    gain += diff
        return entries, gain

    def on_submit(self):
        entries, gain = self._apply(+1)
        if entries:
            make_gl_entries(self, entries, remarks=self.remarks or f"تسوية دفعات {self.party}")
        self.exchange_difference = gain
        self.save(update_fields=["exchange_difference"])

    def on_cancel(self):
        self._apply(-1)
        cancel_gl_entries(self)


class PaymentReconciliationLine(models.Model):
    parent = models.ForeignKey(PaymentReconciliation, on_delete=models.CASCADE, related_name="lines")
    payment = models.ForeignKey(PaymentEntry, models.PROTECT, null=True, blank=True, related_name="reconciliation_lines",
                                verbose_name="سند (دفعة مقدمة)",
                                limit_choices_to={"docstatus": 1, "unallocated_amount__gt": 0})
    sales_return = models.ForeignKey(
        "selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="reconciliation_sources",
        verbose_name="إشعار دائن (مرتجع مبيعات)",
        limit_choices_to={"docstatus": 1, "is_return": True, "outstanding_amount__lt": 0})
    purchase_return = models.ForeignKey(
        "buying.PurchaseInvoice", models.PROTECT, null=True, blank=True, related_name="reconciliation_sources",
        verbose_name="إشعار مدين (مرتجع مشتريات)",
        limit_choices_to={"docstatus": 1, "is_return": True, "outstanding_amount__lt": 0})
    sales_invoice = models.ForeignKey(
        "selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="reconciliation_lines",
        verbose_name="فاتورة المبيعات", limit_choices_to={"docstatus": 1, "outstanding_amount__gt": 0})
    purchase_invoice = models.ForeignKey(
        "buying.PurchaseInvoice", models.PROTECT, null=True, blank=True, related_name="reconciliation_lines",
        verbose_name="فاتورة المشتريات", limit_choices_to={"docstatus": 1, "outstanding_amount__gt": 0})
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2, default=0)

    class Meta:
        verbose_name = "سطر تسوية"
        verbose_name_plural = "سطور التسوية"

    @property
    def source(self):
        return self.payment or self.sales_return or self.purchase_return

    @property
    def target(self):
        return self.sales_invoice or self.purchase_invoice


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


# ---------- الشيكات (أوراق القبض والدفع) ----------

class ChequeDirection(models.TextChoices):
    RECEIVED = "received", "شيك وارد (ورقة قبض)"
    ISSUED = "issued", "شيك صادر (ورقة دفع)"


class ChequeStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    IN_HAND = "in_hand", "في الحافظة"
    DEPOSITED = "deposited", "مودع تحت التحصيل"
    ISSUED = "issued", "صادر لم يُصرف"
    CLEARED = "cleared", "محصّل / مصروف"
    BOUNCED = "bounced", "مرتد"
    CANCELLED = "cancelled", "ملغى"


CHEQUE_ACCOUNTS = {
    ChequeDirection.RECEIVED: ("1108", "أوراق القبض - شيكات تحت التحصيل", AccountType.CHEQUES_RECEIVABLE, RootType.ASSET),
    ChequeDirection.ISSUED: ("2107", "أوراق الدفع - شيكات صادرة", AccountType.CHEQUES_PAYABLE, RootType.LIABILITY),
}


def cheque_account(company, direction):
    """حساب الشيكات للشركة، ويُنشأ تحت الأصول/الخصوم المتداولة إن لم يوجد (للشركات القائمة)."""
    number, name, kind, root = CHEQUE_ACCOUNTS[direction]
    account = Account.all_objects.filter(company=company, account_type=kind, is_group=False, disabled=False).first()
    if account is not None:
        return account
    parent = (Account.all_objects.filter(company=company, number=number[:2], is_group=True).first() or
              Account.all_objects.filter(company=company, root_type=root, is_group=True, parent__isnull=True).first())
    account, _ = Account.all_objects.get_or_create(
        company=company, name=name, defaults={"number": number, "parent": parent, "root_type": root, "account_type": kind})
    return account


class Cheque(SubmittableDocument):
    """شيك وارد من عميل أو صادر لمورد بتاريخ استحقاق: يُسجل سنداً على حساب الأوراق ثم يُحصّل أو يرتد."""

    naming_prefix = "CHQ"

    direction = models.CharField("نوع الشيك", max_length=10, choices=ChequeDirection.choices,
                                 default=ChequeDirection.RECEIVED)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, verbose_name="العميل")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")
    cheque_no = models.CharField("رقم الشيك", max_length=40)
    drawer_bank = models.CharField("البنك المسحوب عليه", max_length=100, blank=True)
    cheque_date = models.DateField("تاريخ الاستحقاق")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    bank_account = models.ForeignKey(
        Account, models.PROTECT, null=True, blank=True, related_name="+", verbose_name="حساب البنك",
        limit_choices_to={"is_group": False, "account_type": AccountType.BANK},
        help_text="الوارد: البنك المودع فيه. الصادر: البنك المسحوب منه. فارغ = أول حساب بنك")
    sales_invoice = models.ForeignKey("selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="+",
                                      verbose_name="سداد فاتورة مبيعات", limit_choices_to={"docstatus": 1,
                                                                                          "outstanding_amount__gt": 0})
    purchase_invoice = models.ForeignKey("buying.PurchaseInvoice", models.PROTECT, null=True, blank=True,
                                         related_name="+", verbose_name="سداد فاتورة مشتريات",
                                         limit_choices_to={"docstatus": 1, "outstanding_amount__gt": 0})
    status = models.CharField("حالة الشيك", max_length=10, choices=ChequeStatus.choices, default=ChequeStatus.DRAFT,
                              editable=False)
    payment = models.ForeignKey(PaymentEntry, models.PROTECT, null=True, blank=True, related_name="cheques",
                                editable=False, verbose_name="سند الاستلام / الصرف")
    clearance_entry = models.ForeignKey(JournalEntry, models.PROTECT, null=True, blank=True, related_name="+",
                                        editable=False, verbose_name="قيد التحصيل")
    cleared_on = models.DateField("تاريخ التحصيل / الصرف", null=True, blank=True, editable=False)
    bounced_on = models.DateField("تاريخ الارتداد", null=True, blank=True, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "شيك"
        verbose_name_plural = "حافظة الشيكات"
        ordering = ["cheque_date", "id"]

    @property
    def party(self):
        return self.customer or self.supplier

    @property
    def document_title(self):
        return self.get_direction_display()

    @property
    def linked_documents(self):
        return [doc for doc in (self.payment, self.clearance_entry) if doc is not None]

    @property
    def is_overdue(self):
        from django.utils import timezone

        return self.status in (ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED, ChequeStatus.ISSUED) and \
            self.cheque_date < timezone.localdate()

    def validate_for_submit(self):
        if flt(self.amount) <= 0:
            raise ValidationError("مبلغ الشيك يجب أن يكون أكبر من صفر.")
        if self.direction == ChequeDirection.RECEIVED and (not self.customer_id or self.supplier_id):
            raise ValidationError("الشيك الوارد يُحدد له عميل فقط.")
        if self.direction == ChequeDirection.ISSUED and (not self.supplier_id or self.customer_id):
            raise ValidationError("الشيك الصادر يُحدد له مورد فقط.")
        if self.direction == ChequeDirection.RECEIVED and self.purchase_invoice_id or \
                self.direction == ChequeDirection.ISSUED and self.sales_invoice_id:
            raise ValidationError("الفاتورة لا تتوافق مع نوع الشيك.")
        duplicate = Cheque.objects.filter(direction=self.direction, cheque_no=self.cheque_no, docstatus=1,
                                          drawer_bank=self.drawer_bank).exclude(pk=self.pk)
        if duplicate.exists():
            raise ValidationError(f"الشيك رقم {self.cheque_no} مسجل مسبقاً ({duplicate.first()}).")

    def on_submit(self):
        received = self.direction == ChequeDirection.RECEIVED
        payment = PaymentEntry.objects.create(
            company=self.company, payment_type=PaymentType.RECEIVE if received else PaymentType.PAY,
            customer=self.customer, supplier=self.supplier, account=cheque_account(self.company, self.direction),
            amount=self.amount, posting_date=self.posting_date, reference_no=self.cheque_no[:50],
            sales_invoice=self.sales_invoice, purchase_invoice=self.purchase_invoice, created_by=self.created_by,
            remarks=f"شيك رقم {self.cheque_no} يستحق {self.cheque_date:%Y-%m-%d}" + (f" — {self.drawer_bank}" if self.drawer_bank else ""))
        payment.calculate_totals()
        payment.save()
        payment.submit()
        self.payment = payment
        self.status = ChequeStatus.IN_HAND if received else ChequeStatus.ISSUED
        self.save(update_fields=["payment", "status"])

    @property
    def available_actions(self):
        if not self.is_submitted:
            return []
        actions = []
        if self.status == ChequeStatus.IN_HAND:
            actions.append(("mark_deposited", "إيداع في البنك"))
        if self.status in (ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED):
            actions += [("clear", "تحصيل الشيك"), ("bounce", "ارتداد الشيك")]
        if self.status == ChequeStatus.ISSUED:
            actions += [("clear", "صرف الشيك من البنك"), ("bounce", "ارتداد / إلغاء الشيك")]
        if self.status == ChequeStatus.CLEARED:
            actions.append(("bounce", "ارتداد بعد التحصيل"))
        return actions

    def _bank(self):
        bank = self.bank_account or Account.objects.filter(account_type=AccountType.BANK, is_group=False,
                                                           disabled=False).first()
        if bank is None:
            raise ValidationError("أضف حساب بنك في دليل الحسابات أو حدده في الشيك.")
        return bank

    def mark_deposited(self, user=None):
        if self.status != ChequeStatus.IN_HAND:
            raise ValidationError("الشيك ليس في الحافظة.")
        self._bank()
        self.status = ChequeStatus.DEPOSITED
        self.save(update_fields=["status"])

    def clear(self, user=None):
        from django.utils import timezone

        if self.status not in (ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED, ChequeStatus.ISSUED):
            raise ValidationError("لا يمكن تحصيل الشيك في حالته الحالية.")
        today = timezone.localdate()
        holding, bank = cheque_account(self.company, self.direction), self._bank()
        je = JournalEntry.objects.create(company=self.company, posting_date=max(today, self.posting_date),
                                         created_by=user, remarks=f"تحصيل الشيك رقم {self.cheque_no} — {self.party}")
        debit, credit = (bank, holding) if self.direction == ChequeDirection.RECEIVED else (holding, bank)
        je.accounts.create(account=debit, debit=self.amount, description=f"شيك {self.cheque_no}")
        je.accounts.create(account=credit, credit=self.amount, description=f"شيك {self.cheque_no}")
        je.submit()
        self.clearance_entry, self.status, self.cleared_on = je, ChequeStatus.CLEARED, je.posting_date
        self.save(update_fields=["clearance_entry", "status", "cleared_on"])

    def bounce(self, user=None):
        """الارتداد: يُلغى سند الشيك فتعود الذمة على الطرف وتعود الفاتورة غير مسددة (وقيد التحصيل إن وُجد)."""
        from django.utils import timezone

        from core.notify import notify_managers

        if self.status not in (ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED, ChequeStatus.ISSUED, ChequeStatus.CLEARED):
            raise ValidationError("لا يمكن ارتداد الشيك في حالته الحالية.")
        if self.clearance_entry_id and self.clearance_entry.is_submitted:
            self.clearance_entry.cancel()
        if self.payment_id and self.payment.is_submitted:
            self.payment.cancel()
        self.status, self.bounced_on = ChequeStatus.BOUNCED, timezone.localdate()
        self.save(update_fields=["status", "bounced_on"])
        notify_managers(self.company, f"ارتد الشيك رقم {self.cheque_no} ({self.amount:,.2f}) — {self.party}",
                        self.get_absolute_url())

    def before_cancel(self):
        if self.status == ChequeStatus.CLEARED:
            raise ValidationError("الشيك محصّل؛ استخدم «ارتداد بعد التحصيل» بدلاً من الإلغاء.")

    def on_cancel(self):
        if self.payment_id and self.payment.is_submitted:
            self.payment.cancel()
        self.status = ChequeStatus.CANCELLED
        self.save(update_fields=["status"])



# ---------- الموازنات التقديرية ----------

class BudgetAction(models.TextChoices):
    WARN = "warn", "تنبيه المدير فقط"
    STOP = "stop", "منع الترحيل"


class BudgetPeriod(models.TextChoices):
    ANNUAL = "annual", "سنوية (إجمالي السنة)"
    MONTHLY = "monthly", "شهرية تراكمية (جزء السنة المنقضي)"


class Budget(TenantModel):
    """موازنة سنة مالية لحسابات المصروفات (أو الإيرادات) لكل فرع/مركز تكلفة، مع رقابة عند الترحيل."""

    name = models.CharField("اسم الموازنة", max_length=100)
    fiscal_year = models.ForeignKey("core.FiscalYear", models.PROTECT, verbose_name="السنة المالية")
    cost_center = models.ForeignKey(CostCenter, models.PROTECT, null=True, blank=True, verbose_name="مركز التكلفة / الفرع",
                                    help_text="فارغ = الشركة كلها. المركز الرئيسي يشمل فروعه")
    control_period = models.CharField("أساس الرقابة", max_length=10, choices=BudgetPeriod.choices,
                                      default=BudgetPeriod.ANNUAL,
                                      help_text="الشهرية: المسموح حتى الشهر الحالي = الموازنة × الأشهر المنقضية ÷ 12")
    action = models.CharField("عند التجاوز", max_length=10, choices=BudgetAction.choices, default=BudgetAction.WARN)
    disabled = models.BooleanField("موقوفة", default=False)

    class Meta:
        verbose_name = "موازنة"
        verbose_name_plural = "الموازنات التقديرية"
        ordering = ["-fiscal_year__start_date", "name"]
        constraints = [tenant_unique("name", model_label="budget")]

    def __str__(self):
        return self.name

    def center_ids(self):
        return self.cost_center.descendant_ids() if self.cost_center_id else None

    def allowed(self, amount, on_date):
        if self.control_period == BudgetPeriod.ANNUAL:
            return flt(amount)
        start = self.fiscal_year.start_date
        months = (on_date.year - start.year) * 12 + on_date.month - start.month + 1
        total = (self.fiscal_year.end_date.year - start.year) * 12 + self.fiscal_year.end_date.month - start.month + 1
        return flt(flt(amount) * min(months, total) / total)

    def actual(self, account, until=None):
        """الفعلي على الحساب (وفروعه) ضمن السنة ومراكز الموازنة: مصروف = مدين − دائن، إيراد = دائن − مدين."""
        qs = GLEntry.objects.filter(is_cancelled=False, account_id__in=account.descendant_ids(),
                                    posting_date__gte=self.fiscal_year.start_date,
                                    posting_date__lte=until or self.fiscal_year.end_date)
        centers = self.center_ids()
        if centers is not None:
            qs = qs.filter(cost_center_id__in=centers)
        agg = qs.aggregate(d=models.Sum("debit"), c=models.Sum("credit"))
        value = flt(agg["d"]) - flt(agg["c"])
        return value if account.root_type == RootType.EXPENSE else -value


class BudgetLine(models.Model):
    parent = models.ForeignKey(Budget, models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, models.PROTECT, verbose_name="الحساب",
                                limit_choices_to={"root_type__in": [RootType.EXPENSE, RootType.INCOME]},
                                help_text="حساب فرعي أو رئيسي (يشمل حساباته الفرعية)")
    amount = models.DecimalField("الموازنة السنوية", max_digits=18, decimal_places=2)

    class Meta:
        verbose_name = "بند موازنة"
        verbose_name_plural = "بنود الموازنة"


# ---------- إعادة تقييم الأرصدة بالعملات الأجنبية ----------

class ExchangeRevaluation(SubmittableDocument):
    """إعادة تقييم الذمم المفتوحة بالعملات الأجنبية بسعر صرف نهاية الفترة (أرباح/خسائر غير محققة).

    يُرحّل الفرق في تاريخ المستند ويُعكس آلياً في اليوم التالي، فيبقى الربح المحقق عند السداد صحيحاً.
    """

    naming_prefix = "FXR"

    total_gain = models.DecimalField("صافي الربح / (الخسارة) غير المحقق", max_digits=18, decimal_places=2, default=0,
                                     editable=False)
    reversal_entry = models.ForeignKey(JournalEntry, models.PROTECT, null=True, blank=True, related_name="+",
                                       editable=False, verbose_name="قيد العكس")

    class Meta(SubmittableDocument.Meta):
        verbose_name = "إعادة تقييم العملات"
        verbose_name_plural = "إعادة تقييم أرصدة العملات الأجنبية"

    @property
    def linked_documents(self):
        return [self.reversal_entry] if self.reversal_entry_id else []

    def fetch_lines(self):
        from buying.models import PurchaseInvoice
        from core.models import ExchangeRate
        from selling.models import SalesInvoice

        self.lines.all().delete()
        for model, field in ((SalesInvoice, "sales_invoice"), (PurchaseInvoice, "purchase_invoice")):
            qs = (model.objects.filter(docstatus=1, posting_date__lte=self.posting_date).exclude(outstanding_amount=0)
                  .exclude(currency=self.company.currency).select_related("currency"))
            for inv in qs:
                rate = ExchangeRate.lookup(self.company, inv.currency, self.posting_date)
                if rate is None:
                    raise ValidationError(f"أضف سعر صرف {inv.currency} حتى {self.posting_date:%Y-%m-%d}.")
                self.lines.create(**{field: inv}, currency=inv.currency, outstanding=inv.outstanding_amount,
                                  invoice_rate=inv.exchange_rate, new_rate=rate)

    def calculate_totals(self):
        if self.is_draft and self.pk and not self.lines.exists():
            self.fetch_lines()
        total = flt(0)
        for line in self.lines.all():
            line.difference = flt(flt(line.outstanding) * (flt(line.new_rate, 6) - flt(line.invoice_rate, 6)))
            line.save(update_fields=["difference"])
            # الذمم المدينة: ارتفاع السعر ربح؛ الدائنة: ارتفاعه خسارة
            total += line.difference if line.sales_invoice_id else -line.difference
        self.total_gain = total

    def validate_for_submit(self):
        if not self.lines.exists():
            raise ValidationError("لا توجد أرصدة مفتوحة بعملات أجنبية لإعادة تقييمها.")

    def _entries(self):
        receivable = self.company.require_account("default_receivable_account")
        payable = self.company.require_account("default_payable_account")
        fx = self.company.require_account("default_exchange_account")
        entries = []
        for line in self.lines.select_related("sales_invoice__customer", "purchase_invoice__supplier"):
            diff = flt(line.difference)
            if not diff:
                continue
            if line.sales_invoice_id:
                entries += [{"account": receivable, "debit": diff, "customer": line.sales_invoice.customer},
                            {"account": fx, "credit": diff}]
            else:
                entries += [{"account": payable, "credit": diff, "supplier": line.purchase_invoice.supplier},
                            {"account": fx, "debit": diff}]
        return entries

    def on_submit(self):
        import datetime

        entries = self._entries()
        if entries:
            make_gl_entries(self, entries, remarks=self.remarks or "إعادة تقييم العملات الأجنبية (غير محققة)")
            je = JournalEntry.objects.create(company=self.company, posting_date=self.posting_date + datetime.timedelta(days=1),
                                             entry_type=JournalEntryType.ADJUSTMENT,
                                             remarks=f"عكس إعادة تقييم العملات {self.name}")
            for e in entries:
                net = flt(e.get("debit")) - flt(e.get("credit"))
                je.accounts.create(account=e["account"], customer=e.get("customer"), supplier=e.get("supplier"),
                                   debit=-net if net < 0 else 0, credit=net if net > 0 else 0)
            je.submit()
            self.reversal_entry = je
            self.save(update_fields=["reversal_entry"])

    def on_cancel(self):
        cancel_gl_entries(self)
        if self.reversal_entry_id and self.reversal_entry.is_submitted:
            self.reversal_entry.cancel()


class ExchangeRevaluationLine(models.Model):
    parent = models.ForeignKey(ExchangeRevaluation, models.CASCADE, related_name="lines")
    sales_invoice = models.ForeignKey("selling.SalesInvoice", models.PROTECT, null=True, blank=True, related_name="+",
                                      verbose_name="فاتورة مبيعات")
    purchase_invoice = models.ForeignKey("buying.PurchaseInvoice", models.PROTECT, null=True, blank=True,
                                         related_name="+", verbose_name="فاتورة مشتريات")
    currency = models.ForeignKey("core.Currency", models.PROTECT, related_name="+", verbose_name="العملة")
    outstanding = models.DecimalField("المتبقي بالعملة", max_digits=18, decimal_places=2)
    invoice_rate = models.DecimalField("سعر الفاتورة", max_digits=18, decimal_places=6)
    new_rate = models.DecimalField("سعر نهاية الفترة", max_digits=18, decimal_places=6)
    difference = models.DecimalField("الفرق بعملة الشركة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "سطر إعادة تقييم"
        verbose_name_plural = "الأرصدة المعاد تقييمها"


# ---------- المصروفات المدفوعة مقدماً والإيرادات المؤجلة ----------

class DeferralKind(models.TextChoices):
    PREPAID = "prepaid", "مصروف مدفوع مقدماً (إيجار سنوي، تأمين…)"
    DEFERRED_REVENUE = "deferred_revenue", "إيراد مؤجل (اشتراك/عقد محصّل مقدماً)"


class DeferralSchedule(SubmittableDocument):
    """توزيع مبلغ مدفوع/محصّل مقدماً على أشهر الاستفادة بقيود شهرية تُرحّل آلياً عند حلول كل شهر."""

    naming_prefix = "DEF"

    kind = models.CharField("النوع", max_length=20, choices=DeferralKind.choices, default=DeferralKind.PREPAID)
    balance_account = models.ForeignKey(
        Account, models.PROTECT, related_name="+", verbose_name="حساب الميزانية",
        limit_choices_to={"is_group": False, "root_type__in": [RootType.ASSET, RootType.LIABILITY]},
        help_text="المدفوع مقدماً: مصروفات مدفوعة مقدماً (أصل). الإيراد المؤجل: إيرادات مقدمة (خصم)")
    pl_account = models.ForeignKey(
        Account, models.PROTECT, related_name="+", verbose_name="حساب المصروف / الإيراد",
        limit_choices_to={"is_group": False, "root_type__in": [RootType.EXPENSE, RootType.INCOME]})
    cost_center = models.ForeignKey(CostCenter, models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False}, verbose_name="مركز التكلفة")
    amount = models.DecimalField("المبلغ الإجمالي", max_digits=18, decimal_places=2)
    start_date = models.DateField("بداية الاستفادة")
    months = models.PositiveSmallIntegerField("عدد الأشهر", default=12)
    posted_amount = models.DecimalField("المرحّل حتى الآن", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "جدول توزيع (مقدم / مؤجل)"
        verbose_name_plural = "المصروفات المقدمة والإيرادات المؤجلة"

    posting_note = "الاعتماد يُنشئ جدول الأقساط؛ يُرحّل كل قسط بقيد عند حلول تاريخه (يومياً من المجدول أو بزر «ترحيل المستحق»)."
    posts_to_ledger = True

    def validate_for_submit(self):
        if flt(self.amount) <= 0 or not self.months:
            raise ValidationError("أدخل المبلغ وعدد الأشهر.")
        expected = (RootType.ASSET, RootType.EXPENSE) if self.kind == DeferralKind.PREPAID else \
            (RootType.LIABILITY, RootType.INCOME)
        if (self.balance_account.root_type, self.pl_account.root_type) != expected:
            raise ValidationError("المدفوع مقدماً: حساب أصل وحساب مصروف. الإيراد المؤجل: حساب خصوم وحساب إيراد.")

    def on_submit(self):
        from core.models import Frequency, add_period
        from core.utils import allocate

        shares = allocate(self.amount, {i: 1 for i in range(self.months)})
        self.entries.all().delete()
        for i in range(self.months):
            self.entries.create(posting_date=add_period(self.start_date, Frequency.MONTHLY, i), amount=shares[i])

    @property
    def available_actions(self):
        from django.utils import timezone

        if self.is_submitted and self.entries.filter(journal_entry__isnull=True,
                                                     posting_date__lte=timezone.localdate()).exists():
            return [("post_due", "ترحيل الأقساط المستحقة")]
        return []

    def post_due(self, user=None, until=None):
        from django.utils import timezone

        until = until or timezone.localdate()
        posted = 0
        for entry in self.entries.filter(journal_entry__isnull=True, posting_date__lte=until).order_by("posting_date"):
            je = JournalEntry.objects.create(company=self.company, posting_date=entry.posting_date,
                                             entry_type=JournalEntryType.ADJUSTMENT, created_by=user,
                                             remarks=f"{self.get_kind_display().split(' (')[0]} {self.name} — {entry.posting_date:%Y-%m}")
            if self.kind == DeferralKind.PREPAID:
                je.accounts.create(account=self.pl_account, debit=entry.amount, cost_center=self.cost_center)
                je.accounts.create(account=self.balance_account, credit=entry.amount)
            else:
                je.accounts.create(account=self.balance_account, debit=entry.amount)
                je.accounts.create(account=self.pl_account, credit=entry.amount, cost_center=self.cost_center)
            je.submit()
            entry.journal_entry = je
            entry.save(update_fields=["journal_entry"])
            self.posted_amount = flt(self.posted_amount + entry.amount)
            posted += 1
        self.save(update_fields=["posted_amount"])
        return posted

    def on_cancel(self):
        for entry in self.entries.filter(journal_entry__isnull=False).select_related("journal_entry"):
            if entry.journal_entry.is_submitted:
                entry.journal_entry.cancel()
        self.posted_amount = 0
        self.save(update_fields=["posted_amount"])


class DeferralEntry(models.Model):
    parent = models.ForeignKey(DeferralSchedule, models.CASCADE, related_name="entries")
    posting_date = models.DateField("التاريخ")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    journal_entry = models.ForeignKey(JournalEntry, models.SET_NULL, null=True, blank=True, related_name="+",
                                      verbose_name="القيد")

    class Meta:
        verbose_name = "قسط توزيع"
        verbose_name_plural = "أقساط التوزيع"
        ordering = ["posting_date"]


# ---------- العهد النقدية ----------

def custody_account(company):
    """حساب العهد النقدية، ويُنشأ تحت الأصول المتداولة إن لم يوجد."""
    account = Account.all_objects.filter(company=company, number="1109", is_group=False).first() or \
        Account.all_objects.filter(company=company, name="العهد النقدية لدى الموظفين").first()
    if account is not None:
        return account
    parent = (Account.all_objects.filter(company=company, number="11", is_group=True).first() or
              Account.all_objects.filter(company=company, root_type=RootType.ASSET, is_group=True,
                                         parent__isnull=True).first())
    return Account.all_objects.create(company=company, number="1109", name="العهد النقدية لدى الموظفين",
                                      parent=parent, root_type=RootType.ASSET)


class CustodyStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    OPEN = "open", "مفتوحة"
    SETTLED = "settled", "مسواة"
    CANCELLED = "cancelled", "ملغاة"


class CashCustody(SubmittableDocument):
    """عهدة نقدية لموظف (مشتريات نثرية، مصاريف سفر…) تُسوّى لاحقاً بالفواتير وردّ الباقي."""

    naming_prefix = "CUS"

    employee = models.ForeignKey("hr.Employee", models.PROTECT, related_name="custodies", verbose_name="الموظف")
    amount = models.DecimalField("مبلغ العهدة", max_digits=18, decimal_places=2)
    paid_from = models.ForeignKey(Account, models.PROTECT, related_name="+", verbose_name="صُرفت من",
                                  limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH,
                                                                                            AccountType.BANK]})
    purpose = models.CharField("الغرض", max_length=200)
    settled_amount = models.DecimalField("المسوّى", max_digits=18, decimal_places=2, default=0, editable=False)
    status = models.CharField("الحالة", max_length=10, choices=CustodyStatus.choices, default=CustodyStatus.DRAFT,
                              editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "عهدة نقدية"
        verbose_name_plural = "العهد النقدية"

    @property
    def remaining(self):
        return flt(self.amount - self.settled_amount)

    @property
    def available_actions(self):
        if self.is_submitted and self.status == CustodyStatus.OPEN:
            return [("make_settlement", "تسوية العهدة")]
        return []

    @property
    def linked_documents(self):
        return list(self.settlements.all()) if self.pk else []

    def validate_for_submit(self):
        if flt(self.amount) <= 0:
            raise ValidationError("مبلغ العهدة يجب أن يكون أكبر من صفر.")

    def on_submit(self):
        make_gl_entries(self, [{"account": custody_account(self.company), "debit": self.amount},
                               {"account": self.paid_from, "credit": self.amount}],
                        remarks=f"عهدة {self.employee}: {self.purpose}")
        self.status = CustodyStatus.OPEN
        self.save(update_fields=["status"])

    def before_cancel(self):
        if self.settlements.filter(docstatus=1).exists():
            raise ValidationError("ألغِ تسويات العهدة أولاً.")

    def on_cancel(self):
        cancel_gl_entries(self)
        self.status = CustodyStatus.CANCELLED
        self.save(update_fields=["status"])

    def refresh_settled(self):
        total = self.settlements.filter(docstatus=1).aggregate(
            t=models.Sum("total_expenses"), r=models.Sum("returned_amount"))
        self.settled_amount = flt(flt(total["t"]) + flt(total["r"]))
        self.status = CustodyStatus.SETTLED if self.settled_amount >= self.amount else CustodyStatus.OPEN
        self.save(update_fields=["settled_amount", "status"])

    def make_settlement(self, user=None):
        if self.status != CustodyStatus.OPEN:
            raise ValidationError("العهدة غير مفتوحة.")
        return CustodySettlement.objects.create(company=self.company, custody=self, created_by=user,
                                                return_account=self.paid_from)


class CustodySettlement(SubmittableDocument):
    """تسوية عهدة: المصروفات بفواتيرها (مع ضريبة المدخلات) والمبلغ المردود نقداً."""

    naming_prefix = "CST"

    custody = models.ForeignKey(CashCustody, models.PROTECT, related_name="settlements", verbose_name="العهدة",
                                limit_choices_to={"status": CustodyStatus.OPEN})
    returned_amount = models.DecimalField("المبلغ المردود نقداً", max_digits=18, decimal_places=2, default=0)
    return_account = models.ForeignKey(Account, models.PROTECT, null=True, blank=True, related_name="+",
                                       verbose_name="رُد إلى",
                                       limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH,
                                                                                                 AccountType.BANK]})
    total_expenses = models.DecimalField("إجمالي المصروفات", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "تسوية عهدة"
        verbose_name_plural = "تسويات العهد"

    def calculate_totals(self):
        self.total_expenses = sum((flt(line.amount) + flt(line.tax_amount) for line in self.lines.all()), flt(0))

    def validate_for_submit(self):
        custody = self.custody
        if custody.status != CustodyStatus.OPEN:
            raise ValidationError("العهدة غير مفتوحة.")
        total = flt(self.total_expenses + flt(self.returned_amount))
        if total <= 0:
            raise ValidationError("أدخل المصروفات أو المبلغ المردود.")
        if total > custody.remaining:
            raise ValidationError(f"إجمالي التسوية ({total}) أكبر من المتبقي في العهدة ({custody.remaining}). "
                                  "الزيادة يدفعها الصندوق للموظف بسند صرف مستقل.")
        if self.returned_amount and not self.return_account_id:
            raise ValidationError("حدد الحساب الذي رُد إليه المبلغ.")

    def on_submit(self):
        entries = []
        for line in self.lines.select_related("account"):
            entries.append({"account": line.account, "debit": line.amount, "cost_center": line.cost_center,
                            "project": line.project, "remarks": line.description or self.custody.purpose})
            if line.tax_amount:
                entries.append({"account": self.company.require_account("default_input_tax_account"),
                                "debit": line.tax_amount, "remarks": f"ضريبة مدخلات {line.bill_no}".strip()})
        if self.returned_amount:
            entries.append({"account": self.return_account, "debit": self.returned_amount, "remarks": "رد باقي العهدة"})
        entries.append({"account": custody_account(self.company),
                        "credit": flt(self.total_expenses + flt(self.returned_amount))})
        make_gl_entries(self, entries, remarks=self.remarks or f"تسوية عهدة {self.custody}")
        self.custody.refresh_settled()

    def on_cancel(self):
        cancel_gl_entries(self)
        self.custody.refresh_settled()


class CustodyExpense(models.Model):
    parent = models.ForeignKey(CustodySettlement, models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, models.PROTECT, related_name="+", verbose_name="حساب المصروف",
                                limit_choices_to={"is_group": False, "root_type": RootType.EXPENSE})
    description = models.CharField("البيان", max_length=150, blank=True)
    bill_no = models.CharField("رقم الفاتورة / الإيصال", max_length=50, blank=True)
    amount = models.DecimalField("المبلغ قبل الضريبة", max_digits=18, decimal_places=2, default=0)
    tax_amount = models.DecimalField("ضريبة القيمة المضافة", max_digits=18, decimal_places=2, default=0)
    cost_center = models.ForeignKey(CostCenter, models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False}, verbose_name="مركز التكلفة")
    project = models.ForeignKey("projects.Project", models.PROTECT, null=True, blank=True, related_name="+",
                                verbose_name="المشروع")

    class Meta:
        verbose_name = "مصروف من العهدة"
        verbose_name_plural = "المصروفات"



# ---------- الزكاة ----------

def zakat_accounts(company):
    """(مصروف الزكاة، الزكاة المستحقة) — يُنشآن إن لم يوجدا."""
    def ensure(number, name, root, parent_number):
        account = Account.all_objects.filter(company=company, number=number, is_group=False).first()
        if account is None:
            parent = (Account.all_objects.filter(company=company, number=parent_number, is_group=True).first() or
                      Account.all_objects.filter(company=company, root_type=root, is_group=True,
                                                 parent__isnull=True).first())
            account = Account.all_objects.create(company=company, number=number, name=name, parent=parent,
                                                 root_type=root)
        return account

    return (ensure("5211", "الزكاة الشرعية", RootType.EXPENSE, "52"),
            ensure("2109", "الزكاة المستحقة", RootType.LIABILITY, "21"))


class ZakatDeclaration(SubmittableDocument):
    """حساب الوعاء الزكوي لسنة مالية من الأرصدة، مع تعديلات يدوية، وترحيل مخصص الزكاة.

    الوعاء = (حقوق الملكية + الخصوم طويلة الأجل والمخصصات + صافي الربح المعدّل + إضافات أخرى)
             − (صافي الأصول الثابتة + الاستثمارات طويلة الأجل + حسميات أخرى)، ولا يقل عن صافي الربح المعدّل.
    """

    naming_prefix = "ZKT"

    fiscal_year = models.ForeignKey("core.FiscalYear", models.PROTECT, verbose_name="السنة المالية")
    rate = models.DecimalField("نسبة الزكاة %", max_digits=6, decimal_places=4, default="2.5776",
                               help_text="2.5776% للسنة الميلادية، و2.5% للسنة الهجرية")
    equity = models.DecimalField("حقوق الملكية أول السنة", max_digits=18, decimal_places=2, default=0, editable=False)
    long_term = models.DecimalField("الخصوم طويلة الأجل والمخصصات", max_digits=18, decimal_places=2, default=0,
                                    editable=False)
    net_profit = models.DecimalField("صافي ربح السنة", max_digits=18, decimal_places=2, default=0, editable=False)
    fixed_assets = models.DecimalField("صافي الأصول الثابتة (حسم)", max_digits=18, decimal_places=2, default=0,
                                       editable=False)
    other_additions = models.DecimalField("إضافات أخرى", max_digits=18, decimal_places=2, default=0,
                                          help_text="مثل: مخصصات غير مقبولة، تبرعات")
    other_deductions = models.DecimalField("حسميات أخرى", max_digits=18, decimal_places=2, default=0,
                                           help_text="مثل: استثمارات في منشآت محلية خاضعة للزكاة، خسائر مرحلة")
    profit_adjustments = models.DecimalField("تعديلات صافي الربح", max_digits=18, decimal_places=2, default=0,
                                             help_text="مصروفات غير جائزة الحسم (+) أو إيرادات معفاة (−)")
    zakat_base = models.DecimalField("الوعاء الزكوي", max_digits=18, decimal_places=2, default=0, editable=False)
    zakat_amount = models.DecimalField("الزكاة المستحقة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "إقرار زكوي"
        verbose_name_plural = "الإقرارات الزكوية"

    def save(self, *args, **kwargs):
        if self.fiscal_year_id:
            self.posting_date = self.fiscal_year.end_date
        super().save(*args, **kwargs)

    @staticmethod
    def _treatment(account, ancestors):
        if account.zakat_treatment:
            return account.zakat_treatment
        if account.root_type == RootType.EQUITY:
            return "add"
        if account.root_type == RootType.LIABILITY and "22" in ancestors:
            return "add"
        if account.account_type in (AccountType.FIXED_ASSET, AccountType.ACCUMULATED_DEPRECIATION):
            return "deduct"
        return "ignore"

    def calculate_totals(self):
        if not self.fiscal_year_id:
            return
        year = self.fiscal_year
        accounts = {a.pk: a for a in Account.objects.all()}

        def ancestors(account):
            numbers, node = set(), account
            while node is not None:
                numbers.add(node.number)
                node = accounts.get(node.parent_id)
            return numbers

        def sums(**filters):
            rows = (GLEntry.objects.filter(is_cancelled=False, **filters).exclude(voucher_type="period-closing")
                    .values("account_id").annotate(d=models.Sum("debit"), c=models.Sum("credit")))
            return {r["account_id"]: flt(r["d"]) - flt(r["c"]) for r in rows}

        equity = long_term = fixed = flt(0)
        opening = sums(posting_date__lt=year.start_date)
        closing = sums(posting_date__lte=year.end_date)
        for account_id, balance in opening.items():
            account = accounts[account_id]
            treatment = self._treatment(account, ancestors(account))
            if treatment == "add" and account.root_type == RootType.EQUITY:
                equity += -balance
        for account_id, balance in closing.items():
            account = accounts[account_id]
            treatment = self._treatment(account, ancestors(account))
            if treatment == "add" and account.root_type == RootType.LIABILITY:
                long_term += -balance
            elif treatment == "deduct":
                fixed += balance
        period = sums(posting_date__gte=year.start_date, posting_date__lte=year.end_date,
                      account__root_type__in=[RootType.INCOME, RootType.EXPENSE])
        profit = -sum(period.values(), flt(0))
        self.equity, self.long_term, self.fixed_assets, self.net_profit = equity, long_term, fixed, profit
        adjusted_profit = flt(profit + flt(self.profit_adjustments))
        base = (equity + long_term + max(adjusted_profit, flt(0)) + flt(self.other_additions)
                - fixed - flt(self.other_deductions))
        self.zakat_base = max(base, adjusted_profit, flt(0))
        from decimal import Decimal

        self.zakat_amount = flt(self.zakat_base * Decimal(str(self.rate)) / 100)

    def validate_for_submit(self):
        if ZakatDeclaration.objects.filter(fiscal_year=self.fiscal_year, docstatus=1).exclude(pk=self.pk).exists():
            raise ValidationError(f"يوجد إقرار زكوي معتمد للسنة {self.fiscal_year}.")
        if self.zakat_amount <= 0:
            raise ValidationError("لا زكاة مستحقة على الوعاء المحسوب.")

    def on_submit(self):
        expense, payable = zakat_accounts(self.company)
        make_gl_entries(self, [{"account": expense, "debit": self.zakat_amount},
                               {"account": payable, "credit": self.zakat_amount}],
                        remarks=f"مخصص الزكاة للسنة {self.fiscal_year}")

    def on_cancel(self):
        cancel_gl_entries(self)
