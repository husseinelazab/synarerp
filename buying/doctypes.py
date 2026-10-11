from buying.models import (PurchaseInvoice, PurchaseInvoiceItem, PurchaseOrder, PurchaseOrderItem, PurchaseReceipt,
                           PurchaseReceiptItem, Supplier)
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="purchase-invoice", model=PurchaseInvoice, module="buying",
    list_fields=["name", "posting_date", "supplier", "bill_no", "is_return", "grand_total", "outstanding_amount"],
    form_fields=["supplier", "bill_no", "posting_date", "due_date", "currency", "exchange_rate", "update_stock", "warehouse",
                 "cost_center", "price_list", "ignore_pricing_rules", "tax_rate", "vat_category", "vat_exemption_reason",
                 "remarks"],
    search_fields=["name", "supplier__name", "bill_no"],
    child=ChildTable(PurchaseInvoiceItem, ["item", "uom", "qty", "rate", "batch_no", "expiry_date"], rate_type="buying"),
    totals=["total", "net_total", "tax_amount", "grand_total", "outstanding_amount"],
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
    child=ChildTable(PurchaseReceiptItem, ["item", "uom", "qty", "rate", "batch_no", "expiry_date"], rate_type="buying"),
    totals=["total_amount"],
))
