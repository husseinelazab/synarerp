from core.registry import ChildTable, DocType, register
from stock.models import UOM, Item, ItemGroup, StockEntry, StockEntryItem, Warehouse

register(DocType(
    slug="item", model=Item, module="stock",
    list_fields=["code", "name", "item_group", "uom", "selling_rate", "is_stock_item"],
    search_fields=["code", "name", "barcode"],
))
register(DocType(
    slug="stock-entry", model=StockEntry, module="stock",
    list_fields=["name", "posting_date", "purpose", "from_warehouse", "to_warehouse", "total_amount"],
    form_fields=["purpose", "posting_date", "from_warehouse", "to_warehouse", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(StockEntryItem, ["item", "qty", "rate"], rate_type="buying"),
    totals=["total_amount"],
))
register(DocType(
    slug="warehouse", model=Warehouse, module="stock",
    list_fields=["name", "address", "disabled"], search_fields=["name"],
))
register(DocType(
    slug="item-group", model=ItemGroup, module="stock",
    list_fields=["name", "parent"], search_fields=["name"],
))
register(DocType(
    slug="uom", model=UOM, module="stock", list_fields=["name"], search_fields=["name"],
))
