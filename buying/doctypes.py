from buying.models import (LandedCost, LandedCostCharge, PurchaseInvoice, PurchaseInvoiceItem, PurchaseOrder,
                           PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem, Supplier)
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="purchase-invoice", model=PurchaseInvoice, module="buying",
    list_fields=["name", "posting_date", "supplier", "bill_no", "is_return", "grand_total", "outstanding_amount"],
    form_fields=["supplier", "bill_no", "posting_date", "due_date", "currency", "exchange_rate", "update_stock", "warehouse",
                 "cost_center", "tax_rate", "vat_category", "vat_exemption_reason",
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
                 "tax_rate", "terms", "remarks"],
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

register(DocType(
    slug="landed-cost", model=LandedCost, module="buying",
    list_fields=["name", "posting_date", "purchase_receipt", "purchase_invoice", "total_charges", "to_cogs"],
    form_fields=["purchase_receipt", "purchase_invoice", "allocation", "posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(LandedCostCharge, ["description", "amount", "supplier", "account"], label="التكاليف",
                     help="لكل تكلفة: المورد (شركة الشحن أو المخلص الجمركي) لتُسجل ديناً له، أو الصندوق/البنك إن دُفعت مباشرة. "
                          "المبالغ قبل ضريبة القيمة المضافة (تُسجل ضريبتها في فاتورة الشاحن أو بقيد)."),
    totals=["total_charges", "to_stock", "to_cogs"],
    detail_template="buying/_landed_cost_detail.html",
))
