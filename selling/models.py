from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.invoice import InvoiceDocument, InvoiceItem
from core.utils import flt
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

    class Meta(InvoiceDocument.Meta):
        verbose_name = "فاتورة مبيعات"
        verbose_name_plural = "فواتير المبيعات"

    def on_submit(self):
        company = self.company
        entries = [{
            "account": company.require_account("default_receivable_account"),
            "debit": self.grand_total, "customer": self.customer,
        }]
        cogs_total = flt(0)
        for row in self.items.select_related("item", "item__income_account"):
            income = row.item.income_account or company.require_account("default_income_account")
            entries.append({"account": income, "credit": row.amount})
            if self.update_stock and row.item.is_stock_item:
                cogs_total += -make_stock_ledger_entry(self, row.item, self.warehouse, -row.qty)
        if self.discount_amount:
            entries.append({"account": company.require_account("default_discount_account"), "debit": self.discount_amount})
        if self.tax_amount:
            entries.append({"account": company.require_account("default_output_tax_account"), "credit": self.tax_amount})
        if cogs_total:
            entries.append({"account": company.require_account("default_cogs_account"), "debit": cogs_total})
            entries.append({"account": company.require_account("default_stock_account"), "credit": cogs_total})
        make_gl_entries(self, entries, remarks=self.remarks or f"فاتورة مبيعات للعميل {self.customer}")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)


class SalesInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(SalesInvoice, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند فاتورة مبيعات"
        verbose_name_plural = "الأصناف"
