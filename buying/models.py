from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.invoice import InvoiceDocument, InvoiceItem
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry


class Supplier(models.Model):
    name = models.CharField("اسم المورد", max_length=200, unique=True)
    tax_id = models.CharField("الرقم الضريبي", max_length=20, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=100, blank=True)
    address = models.TextField("العنوان", blank=True)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "مورد"
        verbose_name_plural = "الموردون"
        ordering = ["name"]

    def __str__(self):
        return self.name


class PurchaseInvoice(InvoiceDocument):
    naming_prefix = "PINV"

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    bill_no = models.CharField("رقم فاتورة المورد", max_length=50, blank=True)

    class Meta(InvoiceDocument.Meta):
        verbose_name = "فاتورة مشتريات"
        verbose_name_plural = "فواتير المشتريات"

    def validate_for_submit(self):
        super().validate_for_submit()
        if self.discount_amount:
            from django.core.exceptions import ValidationError

            raise ValidationError("الخصم في فاتورة المشتريات غير مدعوم حالياً؛ أدخل الأسعار بعد الخصم.")

    def on_submit(self):
        company = self.company
        entries = []
        for row in self.items.select_related("item", "item__expense_account"):
            if self.update_stock and row.item.is_stock_item:
                make_stock_ledger_entry(self, row.item, self.warehouse, row.qty, row.rate)
                account = company.require_account("default_stock_account")
            else:
                account = row.item.expense_account or company.require_account("default_expense_account")
            entries.append({"account": account, "debit": row.amount})
        if self.tax_amount:
            entries.append({"account": company.require_account("default_input_tax_account"), "debit": self.tax_amount})
        entries.append({
            "account": company.require_account("default_payable_account"),
            "credit": self.grand_total, "supplier": self.supplier,
        })
        make_gl_entries(self, entries, remarks=self.remarks or f"فاتورة مشتريات من المورد {self.supplier}")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)


class PurchaseInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند فاتورة مشتريات"
        verbose_name_plural = "الأصناف"
