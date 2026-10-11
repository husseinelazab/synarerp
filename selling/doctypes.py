from core.registry import ChildTable, DocType, register
from selling.models import (Customer, CustomerGroup, DeliveryNote, DeliveryNoteItem, Quotation, QuotationItem, SalesInvoice,
                            SalesInvoiceItem, SalesOrder, SalesOrderItem)

register(DocType(
    slug="sales-invoice", model=SalesInvoice, module="selling",
    list_fields=["name", "posting_date", "customer", "is_return", "grand_total", "outstanding_amount"],
    form_fields=["customer", "posting_date", "due_date", "currency", "exchange_rate", "update_stock", "warehouse", "cost_center",
                 "price_list", "coupon_code", "ignore_pricing_rules", "tax_rate", "vat_category", "vat_exemption_reason",
                 "prices_include_tax", "discount_amount", "loyalty_points", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(SalesInvoiceItem, ["item", "uom", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total", "outstanding_amount"],
))
register(DocType(
    slug="customer", model=Customer, module="selling",
    list_fields=["name", "customer_type", "customer_group", "phone", "city", "tax_id", "credit_limit"],
    search_fields=["name", "phone", "tax_id"],
    detail_template="selling/_customer_credit.html",
))
register(DocType(
    slug="quotation", model=Quotation, module="selling",
    list_fields=["name", "posting_date", "customer", "valid_till", "grand_total", "status"],
    form_fields=["customer", "posting_date", "valid_till", "currency", "exchange_rate", "price_list", "coupon_code",
                 "ignore_pricing_rules", "tax_rate", "prices_include_tax", "discount_amount", "terms", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(QuotationItem, ["item", "uom", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total"],
))
register(DocType(
    slug="sales-order", model=SalesOrder, module="selling",
    list_fields=["name", "posting_date", "customer", "delivery_date", "grand_total", "per_billed", "status"],
    form_fields=["customer", "posting_date", "delivery_date", "currency", "exchange_rate", "warehouse", "cost_center",
                 "price_list", "coupon_code", "ignore_pricing_rules", "tax_rate", "prices_include_tax", "discount_amount",
                 "terms", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(SalesOrderItem, ["item", "uom", "qty", "rate"], rate_type="selling"),
    totals=["total", "discount_amount", "net_total", "tax_amount", "grand_total"],
))
register(DocType(
    slug="delivery-note", model=DeliveryNote, module="selling",
    list_fields=["name", "posting_date", "customer", "warehouse", "total_amount", "status"],
    form_fields=["customer", "posting_date", "warehouse", "cost_center", "currency", "exchange_rate", "tax_rate", "remarks"],
    search_fields=["name", "customer__name"],
    child=ChildTable(DeliveryNoteItem, ["item", "uom", "qty", "rate"], rate_type="selling"),
    totals=["total_amount"],
))
register(DocType(
    slug="customer-group", model=CustomerGroup, module="selling",
    list_fields=["name", "price_list"], search_fields=["name"],
))
