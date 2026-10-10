from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render

from accounts.reports import _period
from stock.models import Bin, Item, StockLedgerEntry, Warehouse


@login_required
def stock_balance(request):
    warehouse = request.GET.get("warehouse", "")
    bins = Bin.objects.select_related("item", "item__uom", "warehouse").exclude(actual_qty=0, stock_value=0)
    if warehouse:
        bins = bins.filter(warehouse_id=warehouse)
    return render(request, "reports/stock_balance.html", {
        "title": "أرصدة المخزون", "bins": bins.order_by("item__code", "warehouse__name"),
        "warehouses": Warehouse.objects.all(), "selected_warehouse": warehouse,
        "total_value": bins.aggregate(v=Sum("stock_value"))["v"] or 0,
    })


@login_required
def stock_ledger(request):
    from_date, to_date = _period(request)
    entries = StockLedgerEntry.objects.filter(
        is_cancelled=False, posting_date__gte=from_date, posting_date__lte=to_date
    ).select_related("item", "warehouse")
    selected = {k: request.GET.get(k, "") for k in ("item", "warehouse")}
    if selected["item"]:
        entries = entries.filter(item_id=selected["item"])
    if selected["warehouse"]:
        entries = entries.filter(warehouse_id=selected["warehouse"])
    return render(request, "reports/stock_ledger.html", {
        "title": "دفتر حركة المخزون", "from_date": from_date, "to_date": to_date, "entries": entries,
        "items": Item.objects.filter(is_stock_item=True), "warehouses": Warehouse.objects.all(), "selected": selected,
    })
