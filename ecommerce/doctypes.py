from core.registry import DocType, register
from ecommerce.models import SkuMapping, StoreConnection

register(DocType(
    slug="store-connection", model=StoreConnection, module="selling",
    list_fields=["name", "platform", "warehouse", "enabled", "last_event_at"],
    search_fields=["name", "store_id"],
    detail_template="ecommerce/_connection_detail.html", detail_exclude=["webhook_secret"],
))
register(DocType(
    slug="sku-mapping", model=SkuMapping, module="selling",
    list_fields=["connection", "sku", "item", "uom"], search_fields=["sku", "item__name", "item__code"],
))
