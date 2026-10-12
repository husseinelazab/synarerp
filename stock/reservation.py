"""حجز المخزون لأوامر البيع المفتوحة: المحجوز = ما لم يُسلّم أو يُفوتر من الأوامر، والمتاح = الرصيد − المحجوز."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import F, Q

from core.utils import flt


def reserved_map(items=None, warehouses=None, exclude_order=None):
    """{(الصنف، المستودع): الكمية المحجوزة بالوحدة الأساسية} من أوامر البيع المعتمدة غير المغلقة."""
    from core.commercial import CommercialStatus
    from selling.models import SalesOrderItem

    rows = SalesOrderItem.objects.filter(
        parent__docstatus=1, parent__warehouse__isnull=False, item__is_stock_item=True,
        parent__status__in=[CommercialStatus.TO_BILL, CommercialStatus.PARTLY_BILLED],
    ).filter(Q(qty__gt=F("delivered_qty")) & Q(qty__gt=F("billed_qty")))
    if items is not None:
        rows = rows.filter(item_id__in=items)
    if warehouses is not None:
        rows = rows.filter(parent__warehouse_id__in=warehouses)
    if exclude_order is not None:
        rows = rows.exclude(parent_id=exclude_order)
    result = {}
    for row in rows.values("item_id", "parent__warehouse_id", "qty", "delivered_qty", "billed_qty",
                           "conversion_factor"):
        remaining = row["qty"] - max(row["delivered_qty"], row["billed_qty"])
        key = (row["item_id"], row["parent__warehouse_id"])
        result[key] = result.get(key, flt(0, 3)) + flt(remaining * Decimal(row["conversion_factor"] or 1), 3)
    return result


def check_reserved(doc, rows, warehouse):
    """يمنع صرف كمية محجوزة لأوامر بيع أخرى (إن فعّلت الشركة «حماية المخزون المحجوز»)."""
    from stock.models import Bin

    if not getattr(doc.company, "protect_reserved_stock", False) or warehouse is None:
        return
    needed = {}
    for row in rows:
        if getattr(row, "order_item_id", None) or not row.item.is_stock_item:
            continue  # سطر من أمر بيع يستهلك حجزه هو
        needed[row.item] = needed.get(row.item, flt(0, 3)) + flt(row.stock_qty, 3)
    if not needed:
        return
    reserved = reserved_map(items=[i.pk for i in needed], warehouses=[warehouse.pk])
    for item, qty in needed.items():
        actual = flt(Bin.objects.filter(item=item, warehouse=warehouse).values_list("actual_qty", flat=True).first()
                     or 0, 3)
        held = reserved.get((item.pk, warehouse.pk), flt(0, 3))
        if held and qty > actual - held:
            raise ValidationError(f"«{item.name}»: المتاح للبيع في «{warehouse}» {max(actual - held, 0)} فقط "
                                  f"(الرصيد {actual} منه {held} محجوز لأوامر بيع). بِع من الأمر أو أغلقه.")
