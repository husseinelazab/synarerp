"""المرحلة الثانية من ربط المتاجر: استيراد منتجات المتجر وربطها بالأصناف، وإرسال كميات المخزون للمتجر."""
from decimal import ROUND_FLOOR, Decimal

from django.db import transaction
from django.utils import timezone

from ecommerce.api import ApiError, client
from ecommerce.models import SkuMapping, StoreProduct


def _match(connection, sku):
    from stock.models import Item

    if not sku:
        return None
    mapping = SkuMapping.objects.filter(connection=connection, sku=sku).select_related("item").first()
    if mapping:
        return mapping.item
    return Item.match_code(sku)[0]


def import_products(connection, create_missing=False):
    """يقرأ كل منتجات المتجر ويربطها بالأصناف بالـ SKU، وينشئ الأصناف الناقصة إن طُلب. يعيد ملخصاً."""
    from stock.models import UOM, Item, ItemGroup

    stats = {"total": 0, "linked": 0, "created": 0, "unlinked": 0}
    group, uom = ItemGroup.objects.first(), UOM.objects.first()
    for row in client(connection).products():
        stats["total"] += 1
        product, _ = StoreProduct.objects.get_or_create(connection=connection, external_id=row["external_id"],
                                                        defaults={"company": connection.company})
        product.sku, product.name = row["sku"][:100], row["name"][:255]
        product.price, product.store_quantity = row["price"], row["quantity"]
        if product.item_id is None:
            product.item = _match(connection, product.sku)
            if product.item is None and create_missing and product.sku and not Item.objects.filter(code=product.sku).exists():
                with transaction.atomic():
                    product.item = Item.objects.create(
                        code=product.sku, name=(product.name or product.sku)[:150], item_group=group, uom=uom,
                        selling_rate=product.price or 0)
                stats["created"] += 1
        product.save()
        stats["linked" if product.item_id else "unlinked"] += 1
    return stats


def available_qty(connection, item):
    from stock.models import Bin

    if not item.is_stock_item:
        return None
    on_hand = Bin.objects.filter(item=item, warehouse=connection.warehouse).values_list("actual_qty", flat=True).first()
    qty = Decimal(on_hand or 0) - Decimal(connection.stock_buffer or 0)
    return max(qty, Decimal(0)).quantize(Decimal(1), rounding=ROUND_FLOOR)


def sync_stock(connection, force=False):
    """يرسل للمتجر كميات المنتجات المربوطة التي تغيرت منذ آخر إرسال. يعيد عدد ما أُرسل."""
    products = list(connection.products.filter(item__isnull=False).select_related("item"))
    changes = []
    for product in products:
        qty = available_qty(connection, product.item)
        if qty is None or (connection.platform == "salla" and not product.sku):
            continue
        if force or product.last_pushed_qty is None or qty != product.last_pushed_qty:
            changes.append((product, qty))
    try:
        if changes:
            client(connection).push_quantities(changes)
    except ApiError as exc:
        connection.last_sync_message = str(exc)[:255]
        connection.save(update_fields=["last_sync_message"])
        raise
    for product, qty in changes:
        product.last_pushed_qty = qty
        product.save(update_fields=["last_pushed_qty", "updated_at"])
    connection.last_stock_sync_at = timezone.now()
    connection.last_sync_message = f"أُرسلت كميات {len(changes)} منتج" if changes else "لا تغييرات في الكميات"
    connection.save(update_fields=["last_stock_sync_at", "last_sync_message"])
    return len(changes)
