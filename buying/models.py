from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.invoice import InvoiceDocument, InvoiceItem
from core.models import TenantModel
from core.tenancy import tenant_unique
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry


class Supplier(TenantModel):
    name = models.CharField("اسم المورد", max_length=200)
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
        constraints = [tenant_unique("name", model_label="supplier")]

    def __str__(self):
        return self.name


class PurchaseInvoice(InvoiceDocument):
    naming_prefix = "PINV"
    return_prefix = "PRET"
    party_field = "supplier"
    return_locked_fields = ["tax_rate", "update_stock", "cost_center", "currency", "exchange_rate", "supplier"]

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    bill_no = models.CharField("رقم فاتورة المورد", max_length=50, blank=True)

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

    def on_submit(self):
        company = self.company
        entries = []
        for row in self.items.select_related("item", "item__expense_account"):
            if self.update_stock and row.item.is_stock_item:
                # المخزون يُقيّم بعملة الشركة: سعر الشراء × سعر الصرف.
                # المرتجع يخرج بسعر التقييم الحالي؛ فرقه عن سعر الشراء يذهب لحساب تسويات المخزون
                base_amount = self.to_base(row.amount)
                value = make_stock_ledger_entry(self, row.item, self.warehouse, row.qty, row.rate * self.exchange_rate)
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

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)
        self.unapply_return_from_original()


class PurchaseInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند فاتورة مشتريات"
        verbose_name_plural = "الأصناف"
