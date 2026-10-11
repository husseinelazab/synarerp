from core.registry import ChildTable, DocType, register
from manufacturing.models import BOM, BOMItem, Production, ProductionItem

register(DocType(
    slug="bom", model=BOM, module="manufacturing",
    list_fields=["item", "quantity", "operating_cost", "is_active", "is_default"],
    search_fields=["item__name", "item__code"],
    child=ChildTable(BOMItem, ["item", "qty"], label="المواد الخام"),
    detail_template="manufacturing/_bom_detail.html",
))
register(DocType(
    slug="production", model=Production, module="manufacturing",
    list_fields=["name", "posting_date", "item", "qty", "total_cost", "unit_cost"],
    form_fields=["bom", "qty", "posting_date", "source_warehouse", "target_warehouse", "batch_no", "expiry_date",
                 "additional_cost", "manual_consumption", "remarks"],
    search_fields=["name", "item__name", "batch_no"],
    child=ChildTable(ProductionItem, ["item", "qty", "batch_no"], label="المواد المستهلكة", min_rows=0,
                     display_fields=["item", "required_qty", "qty", "batch_no", "rate", "amount"],
                     help="اترك الجدول فارغاً واحفظ لتُحسب المواد من قائمة المواد حسب الكمية. لتعديل الكميات المستهلكة فعلاً "
                          "فعّل «تعديل الكميات المستهلكة يدوياً»."),
    totals=["material_cost", "operating_cost", "additional_cost", "total_cost", "unit_cost"],
    detail_template="manufacturing/_production_detail.html",
))
