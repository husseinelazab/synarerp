from buying.models import PurchaseInvoice, PurchaseInvoiceItem, Supplier
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="purchase-invoice", model=PurchaseInvoice, module="buying",
    list_fields=["name", "posting_date", "supplier", "bill_no", "is_return", "grand_total", "outstanding_amount"],
    form_fields=["supplier", "bill_no", "posting_date", "due_date", "currency", "exchange_rate", "update_stock", "warehouse",
                 "cost_center", "tax_rate",
                 "remarks"],
    search_fields=["name", "supplier__name", "bill_no"],
    child=ChildTable(PurchaseInvoiceItem, ["item", "qty", "rate"], rate_type="buying"),
    totals=["total", "net_total", "tax_amount", "grand_total", "outstanding_amount"],
))
register(DocType(
    slug="supplier", model=Supplier, module="buying",
    list_fields=["name", "phone", "city", "tax_id"],
    search_fields=["name", "phone", "tax_id"],
))
