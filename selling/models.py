from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.invoice import InvoiceDocument, InvoiceItem
from core.utils import allocate, flt
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry


class CustomerType(models.TextChoices):
    COMPANY = "company", "شركة / منشأة"
    INDIVIDUAL = "individual", "فرد"


class Customer(models.Model):
    name = models.CharField("اسم العميل", max_length=200, unique=True)
    customer_type = models.CharField("نوع العميل", max_length=20, choices=CustomerType.choices, default=CustomerType.COMPANY)
    tax_id = models.CharField("الرقم الضريبي", max_length=20, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=100, blank=True)
    address = models.TextField("العنوان", blank=True)
    credit_limit = models.DecimalField("حد الائتمان", max_digits=18, decimal_places=2, default=0)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "عميل"
        verbose_name_plural = "العملاء"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SalesInvoice(InvoiceDocument):
    naming_prefix = "SINV"

    customer = models.ForeignKey(Customer, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="العميل")
    prices_include_tax = models.BooleanField("الأسعار شاملة الضريبة", default=False)

    # حقول نقاط البيع
    is_pos = models.BooleanField("فاتورة نقطة بيع", default=False, editable=False)
    pos_shift = models.ForeignKey(
        "pos.POSShift", models.PROTECT, null=True, blank=True, related_name="invoices", editable=False, verbose_name="الوردية"
    )
    paid_cash = models.DecimalField("المدفوع نقداً", max_digits=18, decimal_places=2, default=0, editable=False)
    paid_card = models.DecimalField("المدفوع بالبطاقة", max_digits=18, decimal_places=2, default=0, editable=False)
    change_amount = models.DecimalField("المبلغ المرتجع (الباقي)", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta(InvoiceDocument.Meta):
        verbose_name = "فاتورة مبيعات"
        verbose_name_plural = "فواتير المبيعات"

    @property
    def cash_received(self):
        return flt(self.paid_cash - self.change_amount)

    def validate_for_submit(self):
        super().validate_for_submit()
        if self.is_pos:
            if not self.pos_shift_id or not self.pos_shift.is_open:
                raise ValidationError("لا توجد وردية مفتوحة لهذه الفاتورة.")
            if self.paid_cash < 0 or self.paid_card < 0:
                raise ValidationError("مبالغ الدفع لا يمكن أن تكون سالبة.")
            if self.paid_card > self.grand_total:
                raise ValidationError("مبلغ البطاقة لا يمكن أن يتجاوز إجمالي الفاتورة.")
            if flt(self.paid_cash + self.paid_card) < self.grand_total:
                raise ValidationError("المبلغ المدفوع أقل من إجمالي الفاتورة.")

    def on_submit(self):
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
                cogs_total += -make_stock_ledger_entry(self, row.item, self.warehouse, -row.qty)
        # الإيراد = الصافي قبل الضريبة + الخصم (قبل الضريبة)، موزعاً على حسابات الإيراد
        discount = self.discount_excluding_tax
        for account, amount in allocate(self.net_total + discount, income_by_account).items():
            entries.append({"account": account, "credit": amount})
        if discount:
            entries.append({"account": company.require_account("default_discount_account"), "debit": discount})
        if self.tax_amount:
            entries.append({"account": company.require_account("default_output_tax_account"), "credit": self.tax_amount})
        if cogs_total:
            entries.append({"account": company.require_account("default_cogs_account"), "debit": cogs_total})
            entries.append({"account": company.require_account("default_stock_account"), "credit": cogs_total})
        make_gl_entries(self, entries, remarks=self.remarks or f"فاتورة مبيعات للعميل {self.customer}")
        if self.is_pos:
            self._post_pos_payment()

    def _post_pos_payment(self):
        profile = self.pos_shift.profile
        make_gl_entries(self, [
            {"account": profile.cash_account, "debit": self.cash_received},
            {"account": profile.card_account, "debit": self.paid_card},
            {"account": self.company.require_account("default_receivable_account"),
             "credit": self.grand_total, "customer": self.customer},
        ], remarks=f"تحصيل نقطة البيع - {self.pos_shift}")
        self.outstanding_amount = 0
        self.save(update_fields=["outstanding_amount"])

    def before_cancel(self):
        super().before_cancel()
        if self.is_pos and not self.pos_shift.is_open:
            raise ValidationError("لا يمكن إلغاء فاتورة نقطة بيع من وردية مغلقة.")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        if self.is_pos:
            self.outstanding_amount = self.grand_total
            self.save(update_fields=["outstanding_amount"])


class SalesInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(SalesInvoice, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند فاتورة مبيعات"
        verbose_name_plural = "الأصناف"
