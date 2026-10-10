from core.registry import DocType, register
from pos.models import POSProfile

register(DocType(
    slug="pos-profile", model=POSProfile, module="pos",
    list_fields=["name", "warehouse", "cash_account", "prices_include_tax", "disabled"],
    search_fields=["name"],
))
