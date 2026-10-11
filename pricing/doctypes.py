from core.registry import ChildTable, DocType, register
from pricing.models import ItemPrice, LoyaltyPointEntry, LoyaltyProgram, PriceList, PricingRule

register(DocType(
    slug="price-list", model=PriceList, module="selling",
    list_fields=["name", "kind", "currency", "is_default", "disabled"],
    form_fields=["name", "kind", "currency", "is_default", "disabled"],
    search_fields=["name"],
    child=ChildTable(ItemPrice, ["item", "uom", "rate", "valid_from", "valid_upto"], label="أسعار الأصناف",
                     fk_name="price_list", min_rows=0,
                     help="الأصناف غير المدرجة تُسعّر من بطاقة الصنف. لاستيراد أسعار كثيرة استخدم «أسعار الأصناف» ← استيراد."),
))
register(DocType(
    slug="item-price", model=ItemPrice, module="selling",
    list_fields=["price_list", "item", "uom", "rate", "valid_from", "valid_upto"],
    search_fields=["item__name", "item__code", "price_list__name"], show_in_menu=False,
))
register(DocType(
    slug="pricing-rule", model=PricingRule, module="selling",
    list_fields=["name", "kind", "apply_on", "item", "item_group", "discount_type", "min_qty", "coupon_code",
                 "valid_upto", "disabled"],
    search_fields=["name", "coupon_code", "item__name"],
))
register(DocType(
    slug="loyalty-program", model=LoyaltyProgram, module="selling",
    list_fields=["name", "amount_per_point", "point_value", "expiry_days", "auto_enroll", "disabled"],
    search_fields=["name"],
))
register(DocType(
    slug="loyalty-point-entry", model=LoyaltyPointEntry, module="selling",
    list_fields=["posting_date", "customer", "invoice", "points", "expiry_date"],
    search_fields=["customer__name", "invoice__name"], show_in_menu=False,
))
