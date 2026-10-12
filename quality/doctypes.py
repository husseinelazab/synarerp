from core.registry import ChildTable, DocType, register
from quality.models import QualityInspection, QualityParameter, QualityReading, QualityTemplate

register(DocType(
    slug="quality-template", model=QualityTemplate, module="stock",
    list_fields=["name"], search_fields=["name"],
    child=ChildTable(QualityParameter, ["name", "min_value", "max_value", "criteria"], label="معايير الفحص"),
))
register(DocType(
    slug="quality-inspection", model=QualityInspection, module="stock",
    list_fields=["name", "posting_date", "stage", "item", "purchase_receipt", "production", "result"],
    form_fields=["stage", "item", "template", "purchase_receipt", "purchase_invoice", "production", "posting_date",
                 "sample_size", "inspector_notes"],
    search_fields=["name", "item__name"],
    child=ChildTable(QualityReading, ["parameter", "min_value", "max_value", "criteria", "value", "manual_result"],
                     label="القراءات", min_rows=0,
                     display_fields=["parameter", "min_value", "max_value", "criteria", "value", "manual_result", "passed"],
                     help="احفظ الفحص فارغاً لتُجلب معايير نموذج فحص الصنف، ثم أدخل القراءات أو حكم الفاحص."),
))
