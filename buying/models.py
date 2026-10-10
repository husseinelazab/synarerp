from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.commercial import CommercialStatus, OrderDocument, OrderItem, update_order_billing
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


class PurchaseOrder(OrderDocument):
    naming_prefix = "PO"
    party_field = "supplier"

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    schedule_date = models.DateField("تاريخ التوريد المتوقع", null=True, blank=True)
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+",
                                  verbose_name="مستودع الاستلام")
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True, related_name="+",
                                    limit_choices_to={"is_group": False, "disabled": False},
                                    verbose_name="مركز التكلفة / الفرع")

    class Meta(OrderDocument.Meta):
        verbose_name = "أمر شراء"
        verbose_name_plural = "أوامر الشراء"

    def invoice_model(self):
        return PurchaseInvoice

    @property
    def available_actions(self):
        if self.is_open_for_billing:
            return [("make_invoice", "إنشاء فاتورة مشتريات"), ("close_order", "إغلاق الأمر")]
        if self.status == CommercialStatus.CLOSED:
            return [("reopen_order", "إعادة فتح الأمر")]
        return []

    @property
    def linked_documents(self):
        return list(self.invoices.all())

    def validate_for_submit(self):
        super().validate_for_submit()
        if self.discount_amount or self.prices_include_tax:
            raise ValidationError("أوامر الشراء تُدخل بأسعار صافية غير شاملة للضريبة وبدون خصم إجمالي.")


class PurchaseOrderItem(OrderItem):
    parent = models.ForeignKey(PurchaseOrder, models.CASCADE, related_name="items")

    class Meta:
        verbose_name = "بند أمر شراء"
        verbose_name_plural = "الأصناف"


class PurchaseInvoice(InvoiceDocument):
    naming_prefix = "PINV"
    return_prefix = "PRET"
    party_field = "supplier"
    return_locked_fields = InvoiceDocument.return_locked_fields + ["supplier"]

    supplier = models.ForeignKey(Supplier, models.PROTECT, limit_choices_to={"disabled": False}, verbose_name="المورد")
    bill_no = models.CharField("رقم فاتورة المورد", max_length=50, blank=True)
    order = models.ForeignKey(PurchaseOrder, models.PROTECT, null=True, blank=True, related_name="invoices",
                              editable=False, verbose_name="أمر الشراء")

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

    @property
    def linked_documents(self):
        return [self.order] if self.order_id else []

    def on_submit(self):
        update_order_billing(self, +1)
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
        update_order_billing(self, -1)


class PurchaseInvoiceItem(InvoiceItem):
    parent = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name="items")
    order_item = models.ForeignKey(PurchaseOrderItem, models.PROTECT, null=True, blank=True, editable=False,
                                   related_name="invoice_items", verbose_name="بند أمر الشراء")

    class Meta:
        verbose_name = "بند فاتورة مشتريات"
        verbose_name_plural = "الأصناف"
