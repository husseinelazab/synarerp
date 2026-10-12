from buying.models import (LandedCostCharge, MaterialRequest, MaterialRequestItem, SupplierQuotation,
                           SupplierQuotationItem, LandedCostVoucher, PurchaseInvoice, PurchaseInvoiceItem, PurchaseOrder, PurchaseOrderItem, PurchaseReceipt,
                           PurchaseReceiptItem, Supplier)
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="purchase-invoice", model=PurchaseInvoice, module="buying",
    list_fields=["name", "posting_date", "supplier", "bill_no", "is_return", "grand_total", "outstanding_amount"],
    form_fields=["supplier", "bill_no", "posting_date", "payment_terms", "due_date", "currency", "exchange_rate", "update_stock", "warehouse",
                 "cost_center", "project", "price_list", "ignore_pricing_rules", "tax_rate", "vat_category", "vat_exemption_reason",
                 "remarks"],
    search_fields=["name", "supplier__name", "bill_no"],
    child=ChildTable(PurchaseInvoiceItem, ["item", "uom", "qty", "rate", "batch_no", "expiry_date", "serial_nos"],
                     rate_type="buying"),
    totals=["total", "net_total", "tax_amount", "grand_total", "outstanding_amount"],
    detail_template="accounts/_payment_schedule.html",
))
register(DocType(
    slug="supplier", model=Supplier, module="buying",
    list_fields=["name", "phone", "city", "tax_id"],
    search_fields=["name", "phone", "tax_id"],
))
register(DocType(
    slug="purchase-order", model=PurchaseOrder, module="buying",
    list_fields=["name", "posting_date", "supplier", "schedule_date", "grand_total", "per_billed", "status"],
    form_fields=["supplier", "posting_date", "schedule_date", "currency", "exchange_rate", "warehouse", "cost_center",
                 "price_list", "ignore_pricing_rules", "tax_rate", "terms", "remarks"],
    search_fields=["name", "supplier__name"],
    child=ChildTable(PurchaseOrderItem, ["item", "uom", "qty", "rate"], rate_type="buying"),
    totals=["total", "net_total", "tax_amount", "grand_total"],
))
register(DocType(
    slug="purchase-receipt", model=PurchaseReceipt, module="buying",
    list_fields=["name", "posting_date", "supplier", "warehouse", "total_amount", "status"],
    form_fields=["supplier", "supplier_delivery_no", "posting_date", "warehouse", "cost_center", "currency",
                 "exchange_rate", "tax_rate", "remarks"],
    search_fields=["name", "supplier__name", "supplier_delivery_no"],
    child=ChildTable(PurchaseReceiptItem, ["item", "uom", "qty", "rate", "batch_no", "expiry_date", "serial_nos"],
                     rate_type="buying"),
    totals=["total_amount"],
))
register(DocType(
    slug="landed-cost", model=LandedCostVoucher, module="buying",
    list_fields=["name", "posting_date", "purchase_receipt", "purchase_invoice", "total_charges", "capitalized"],
    form_fields=["purchase_receipt", "purchase_invoice", "posting_date", "basis", "remarks"],
    search_fields=["name", "purchase_receipt__name", "purchase_invoice__name", "remarks"],
    child=ChildTable(LandedCostCharge, ["description", "account", "amount"], label="التكاليف الإضافية",
                     help="كل تكلفة تُقيد دائنة على حسابها ومدينة على المخزون. سجّل فاتورة شركة الشحن أو المخلص الجمركي "
                          "على حساب مصروف مؤقت ثم اختر نفس الحساب هنا لنقلها إلى تكلفة البضاعة."),
    totals=["total_charges", "capitalized", "expensed"],
))
register(DocType(
    slug="material-request", model=MaterialRequest, module="buying",
    list_fields=["name", "posting_date", "purpose", "warehouse", "required_by", "per_ordered", "status"],
    form_fields=["purpose", "posting_date", "warehouse", "from_warehouse", "required_by", "department", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(MaterialRequestItem, ["item", "uom", "qty", "notes"], label="الأصناف المطلوبة",
                     display_fields=["item", "uom", "qty", "ordered_qty", "notes"]),
    detail_template="buying/_quotation_comparison.html",
))
register(DocType(
    slug="supplier-quotation", model=SupplierQuotation, module="buying",
    list_fields=["name", "posting_date", "supplier", "valid_till", "grand_total", "status"],
    form_fields=["supplier", "posting_date", "valid_till", "lead_days", "warehouse", "currency", "exchange_rate",
                 "tax_rate", "terms", "remarks"],
    search_fields=["name", "supplier__name"],
    child=ChildTable(SupplierQuotationItem, ["item", "uom", "qty", "rate"], rate_type="buying"),
    totals=["total", "net_total", "tax_amount", "grand_total"],
))
