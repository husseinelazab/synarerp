from core.registry import ChildTable, DocType, register
from selling.models import Customer, SalesInvoice, SalesInvoiceItem

register(DocType(
    slug="sales-invoice", model=SalesInvoice, module="selling",
    list_fields=["name", "posting_date", "customer", "grand_total", "outstanding_amount"],
    form_fields=["customer", "posting_date", "due_date", "update_stock", "warehouse", "tax_rate", "prices_include_tax", "discount_amount", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(SalesInvoiceItem, ["item", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total", "outstanding_amount"],
))
register(DocType(
    slug="customer", model=Customer, module="selling",
    list_fields=["name", "customer_type", "phone", "city", "tax_id"],
    search_fields=["name", "phone", "tax_id"],
))
