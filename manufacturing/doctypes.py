from core.registry import ChildTable, DocType, register
from manufacturing.models import (BOM, BOMItem, Production, ProductionItem, Repack, RepackItem, RepackTemplate,
                                  RepackTemplateItem, Routing, RoutingOperation, Workstation)

register(DocType(
    slug="bom", model=BOM, module="manufacturing",
    list_fields=["item", "quantity", "operating_cost", "routing", "is_active", "is_default"],
    search_fields=["item__name", "item__code"],
    child=ChildTable(BOMItem, ["item", "qty", "scrap_percent"], label="المواد الخام"),
    detail_template="manufacturing/_bom_detail.html",
))
register(DocType(
    slug="production", model=Production, module="manufacturing",
    list_fields=["name", "posting_date", "item", "qty", "rejected_qty", "total_cost", "unit_cost"],
    form_fields=["bom", "qty", "posting_date", "source_warehouse", "target_warehouse", "batch_no", "expiry_date",
                 "rejected_qty", "scrap_qty", "additional_cost", "manual_consumption", "remarks"],
    search_fields=["name", "item__name", "batch_no"],
    child=ChildTable(ProductionItem, ["item", "qty", "batch_no"], label="المواد المستهلكة", min_rows=0,
                     display_fields=["item", "required_qty", "qty", "batch_no", "rate", "amount"],
                     help="اترك الجدول فارغاً واحفظ لتُحسب المواد من قائمة المواد حسب الكمية (شاملة نسب الهالك). "
                          "لتعديل الكميات المستهلكة فعلاً فعّل «تعديل الكميات المستهلكة يدوياً»."),
    totals=["material_cost", "operating_cost", "additional_cost", "scrap_value", "total_cost", "unit_cost"],
    detail_template="manufacturing/_production_detail.html",
))
register(DocType(
    slug="workstation", model=Workstation, module="manufacturing",
    list_fields=["name", "hourly_rate"], search_fields=["name"],
))
register(DocType(
    slug="routing", model=Routing, module="manufacturing",
    list_fields=["name"], search_fields=["name"],
    child=ChildTable(RoutingOperation, ["operation", "workstation", "minutes"], label="العمليات"),
    detail_template="manufacturing/_routing_detail.html",
))
register(DocType(
    slug="repack", model=Repack, module="manufacturing",
    list_fields=["name", "posting_date", "template", "source_qty", "total_cost"],
    form_fields=["template", "source_qty", "posting_date", "source_warehouse", "target_warehouse", "cost_basis",
                 "additional_cost", "remarks"],
    search_fields=["name", "template__name", "items__item__name"],
    child=ChildTable(RepackItem, ["row_type", "item", "qty", "for_item", "cost_percent", "batch_no"],
                     label="الأصناف", min_rows=0,
                     display_fields=["row_type", "item", "qty", "for_item", "rate", "amount"],
                     help="مع القالب: اترك الجدول فارغاً واحفظ ليُعبأ تلقائياً، ثم عدّل الكميات بالفعلي. "
                          "«مادة تُفكك» تكلفتها تُوزع على المنتجات، و«تغليف لمنتج» يُحمّل على المنتج المحدد فقط."),
    totals=["input_cost", "packaging_cost", "additional_cost", "total_cost"],
    detail_template="manufacturing/_repack_detail.html",
))
register(DocType(
    slug="repack-template", model=RepackTemplate, module="manufacturing",
    list_fields=["name", "source_item", "source_qty", "cost_basis", "is_active"],
    search_fields=["name", "source_item__name"],
    child=ChildTable(RepackTemplateItem, ["row_type", "item", "qty", "for_item", "cost_percent"],
                     label="المنتجات والتغليف",
                     help="لكل منتج سطر «منتج ناتج» بالعدد الذي يخرج من الكمية أعلاه، ولكل تغليف سطر «تغليف لمنتج» "
                          "بالكمية الإجمالية مع اختيار المنتج الذي يُغلّف به."),
))
