from core.models import Company, Currency, FiscalYear
from core.registry import DocType, register

register(DocType(
    slug="company", model=Company, module="setup",
    list_fields=["name", "abbr", "tax_id", "currency"], search_fields=["name", "tax_id"],
))
register(DocType(
    slug="currency", model=Currency, module="setup",
    list_fields=["code", "name", "symbol"], search_fields=["code", "name"],
))
register(DocType(
    slug="fiscal-year", model=FiscalYear, module="setup",
    list_fields=["name", "start_date", "end_date", "is_closed"], search_fields=["name"],
))
