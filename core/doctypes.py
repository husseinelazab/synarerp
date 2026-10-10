from core.models import FiscalYear
from core.registry import DocType, register

# الشركة نفسها تُدار من صفحة «إعدادات الشركة»، والعملات تديرها إدارة المنصة
register(DocType(
    slug="fiscal-year", model=FiscalYear, module="setup",
    list_fields=["name", "start_date", "end_date", "is_closed"], search_fields=["name"],
))
