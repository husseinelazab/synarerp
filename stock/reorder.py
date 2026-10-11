"""حد إعادة الطلب: الأصناف التي نزل رصيدها المتوقع (المتاح + المطلوب من الموردين) إلى حدها، مع معدل البيع اليومي."""
import datetime
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from core.utils import flt


def daily_sales(days=30, warehouses=None):
    """متوسط الكمية المباعة يومياً لكل صنف (بالوحدة الأساسية) من دفتر المخزون: صادر المبيعات وأذون التسليم ونقطة البيع."""
    from stock.models import StockLedgerEntry

    since = timezone.localdate() - datetime.timedelta(days=days)
    qs = StockLedgerEntry.objects.filter(is_cancelled=False, posting_date__gt=since,
                                         voucher_type__in=["sales-invoice", "delivery-note"])
    if warehouses:
        qs = qs.filter(warehouse__in=warehouses)
    return {r["item_id"]: flt(-r["q"] / days, 3) for r in qs.values("item_id").annotate(q=Sum("actual_qty"))
            if r["q"] and r["q"] < 0}


def reorder_rows(warehouses=None, only_below=True, lead_days=7, sales_days=30):
    from manufacturing.planning import last_supplier, open_purchase_qty, stock_available
    from stock.models import Item, Warehouse

    warehouses = warehouses or list(Warehouse.objects.filter(disabled=False))
    velocity = daily_sales(sales_days, warehouses)
    on_order = open_purchase_qty()
    items = Item.objects.filter(disabled=False, is_stock_item=True).select_related("uom", "item_group")
    if only_below:
        items = items.filter(reorder_level__gt=0)
    rows = []
    for item in items.order_by("code"):
        available = stock_available(item, warehouses)
        ordered = flt(on_order.get(item.pk, 0), 3)
        projected = flt(available + ordered, 3)
        per_day = velocity.get(item.pk, Decimal(0))
        level = flt(item.reorder_level, 3)
        below = bool(level) and projected <= level
        if only_below and not below:
            continue
        suggested = Decimal(0)
        if below:
            suggested = flt(item.reorder_qty, 3) if item.reorder_qty else flt(level - projected, 3)
            suggested = max(suggested, flt(level - projected, 3))
        rows.append({
            "item": item, "available": available, "on_order": ordered, "projected": projected, "level": level,
            "below": below, "suggested": suggested, "per_day": per_day,
            "cover_days": int(available / per_day) if per_day else None,
            "suggested_level": flt(per_day * lead_days, 3) if per_day else None,
            "supplier": last_supplier(item) if below else None, "rate": item.buying_rate,
            "amount": flt(suggested * item.buying_rate),
        })
    return rows
