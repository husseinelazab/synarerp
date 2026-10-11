from core.registry import ChildTable, DocType, register
from stock.models import (UOM, Batch, Item, ItemGroup, OpeningStock, OpeningStockItem, Scale, StockCount, StockCountItem,
                          StockEntry, StockEntryItem, Warehouse)

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
    child=ChildTable(StockEntryItem, ["item", "qty", "rate", "batch_no", "expiry_date"], rate_type="buying"),
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
register(DocType(
    slug="opening-stock", model=OpeningStock, module="stock",
    list_fields=["name", "posting_date", "warehouse", "total_amount"], form_fields=["warehouse", "posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(OpeningStockItem, ["item", "qty", "rate", "batch_no", "expiry_date"], label="الأصناف", rate_type="buying"),
    totals=["total_amount"],
))
register(DocType(
    slug="stock-count", model=StockCount, module="stock",
    list_fields=["name", "posting_date", "warehouse", "item_group", "difference_value"],
    form_fields=["warehouse", "item_group", "posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(StockCountItem, ["item", "counted_qty", "valuation_rate"], label="الأصناف المعدودة", min_rows=0,
                     display_fields=["item", "system_qty", "counted_qty", "difference_qty", "valuation_rate",
                                     "difference_value"],
                     help="اترك الجدول فارغاً واحفظ لتُجلب أصناف المستودع بكمياتها الدفترية، ثم اطبع ورقة الجرد وعدّل "
                          "«الكمية المعدودة» لما يختلف فقط. الفروقات تُرحّل لحساب تسويات المخزون عند الاعتماد."),
    totals=["difference_value"],
))
register(DocType(
    slug="scale", model=Scale, module="stock",
    list_fields=["name", "brand", "driver", "enabled", "last_sync_at", "last_sync_message"],
    search_fields=["name", "brand"],
))
register(DocType(
    slug="batch", model=Batch, module="stock",
    list_fields=["batch_no", "item", "manufacturing_date", "expiry_date", "reference"],
    search_fields=["batch_no", "item__name", "item__code"],
))
