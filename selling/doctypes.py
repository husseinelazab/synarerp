from core.registry import ChildTable, DocType, register
from selling.models import (Customer, Quotation, QuotationItem, SalesInvoice, SalesInvoiceItem, SalesOrder,
                            SalesOrderItem)

register(DocType(
    slug="sales-invoice", model=SalesInvoice, module="selling",
    list_fields=["name", "posting_date", "customer", "is_return", "grand_total", "outstanding_amount"],
    form_fields=["customer", "posting_date", "due_date", "currency", "exchange_rate", "update_stock", "warehouse", "cost_center",
                 "tax_rate", "prices_include_tax", "discount_amount", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(SalesInvoiceItem, ["item", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total", "outstanding_amount"],
))
register(DocType(
    slug="customer", model=Customer, module="selling",
    list_fields=["name", "customer_type", "phone", "city", "tax_id"],
    search_fields=["name", "phone", "tax_id"],
))
register(DocType(
    slug="quotation", model=Quotation, module="selling",
    list_fields=["name", "posting_date", "customer", "valid_till", "grand_total", "status"],
    form_fields=["customer", "posting_date", "valid_till", "currency", "exchange_rate", "tax_rate", "prices_include_tax",
                 "discount_amount", "terms", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(QuotationItem, ["item", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total"],
))
register(DocType(
    slug="sales-order", model=SalesOrder, module="selling",
    list_fields=["name", "posting_date", "customer", "delivery_date", "grand_total", "per_billed", "status"],
    form_fields=["customer", "posting_date", "delivery_date", "currency", "exchange_rate", "warehouse", "cost_center",
                 "tax_rate", "prices_include_tax", "discount_amount", "terms", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(SalesOrderItem, ["item", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total"],
))
