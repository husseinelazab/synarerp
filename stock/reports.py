from decimal import Decimal

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


# ---------------- الربحية والتسعير ----------------

def _csv(filename, header, rows):
    import csv

    from django.http import HttpResponse

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


@login_required
def gross_profit_report(request):
    from accounts.models import CostCenter
    from selling.models import Customer
    from stock.models import ItemGroup
    from stock.profit import GROUPINGS, gross_profit

    from_date, to_date = _period(request)
    group_by = request.GET.get("group_by") if request.GET.get("group_by") in GROUPINGS else "item"
    filters = {k: request.GET.get(k) or None for k in ("item_group", "customer", "cost_center")}
    rows, totals = gross_profit(from_date, to_date, group_by, **filters)
    sort = request.GET.get("sort", "profit")
    if sort == "margin":
        rows.sort(key=lambda r: (r["margin"] is None, r["margin"] if r["margin"] is not None else 0))
    elif sort == "sales":
        rows.sort(key=lambda r: -r["sales"])
    elif sort == "loss":
        rows = [r for r in rows if r["profit"] < 0]
        rows.sort(key=lambda r: r["profit"])
    else:
        rows.sort(key=lambda r: -r["profit"])
    if request.GET.get("format") == "csv":
        return _csv("gross-profit.csv", [GROUPINGS[group_by], "الكمية", "صافي المبيعات", "التكلفة", "مجمل الربح",
                                         "هامش الربح %", "نسبة الربح على التكلفة %"],
                    [[r["label"], r["qty"], r["sales"], r["cost"], r["profit"], r["margin"] if r["margin"] is not None else "",
                      r["markup"] if r["markup"] is not None else ""] for r in rows])
    return render(request, "reports/gross_profit.html", {
        "title": "تقرير الربحية (مجمل الربح)", "from_date": from_date, "to_date": to_date, "rows": rows,
        "totals": totals, "group_by": group_by, "groupings": GROUPINGS, "sort": sort, "filters": filters,
        "item_groups": ItemGroup.objects.order_by("name"), "customers": Customer.objects.order_by("name"),
        "cost_centers": CostCenter.objects.filter(is_group=False).order_by("name"),
        "query": request.GET.urlencode(),
    })


@login_required
def pricing_analysis(request):
    import datetime

    from django.utils import timezone

    from core.utils import flt
    from stock.models import ItemGroup
    from stock.pricing import selling_from_net, selling_net
    from stock.profit import gross_profit
    from stock.tools import prices_include_tax_default

    company = request.company
    try:
        target = max(Decimal(0), min(Decimal(request.GET.get("target") or 20), Decimal(95)))
    except Exception:  # noqa: BLE001
        target = Decimal(20)
    try:
        days = max(1, min(int(request.GET.get("days") or 90), 3650))
    except ValueError:
        days = 90
    group = request.GET.get("item_group") or ""
    status_filter = request.GET.get("status") or ""
    q = (request.GET.get("q") or "").strip()
    today = timezone.localdate()
    sold = {r["key"]: r for r in gross_profit(today - datetime.timedelta(days=days), today, "item")[0]}
    bins = {}
    for row in Bin.objects.values("item_id").annotate(v=Sum("stock_value"), qty=Sum("actual_qty")):
        bins[row["item_id"]] = row
    items = Item.objects.filter(disabled=False).select_related("item_group", "uom").order_by("code")
    if group:
        items = items.filter(item_group_id=group)
    if q:
        items = items.filter(name__icontains=q) | items.filter(code__icontains=q)
    rows, counts = [], {"loss": 0, "low": 0, "ok": 0, "unknown": 0}
    for item in items:
        b = bins.get(item.id)
        avg = flt(b["v"] / b["qty"]) if b and b["qty"] and b["qty"] > 0 else None
        last = flt(item.buying_rate) if item.buying_rate else None
        cost = last or avg  # سعر الاستبدال (آخر شراء) هو أساس قرار التسعير
        net = selling_net(item, company)
        margin = flt((net - cost) * 100 / net) if cost and net else None
        if not item.is_stock_item and not cost:
            status = "unknown"
        elif not cost or not net:
            status = "unknown"
        elif net < cost:
            status = "loss"
        elif margin < target:
            status = "low"
        else:
            status = "ok"
        counts[status] += 1
        suggested = None
        if cost and status in ("loss", "low") and target < 100:
            suggested = selling_from_net(flt(cost * 100 / (100 - target)), item, company)
        s = sold.get(item.id)
        row = {"item": item, "selling": item.selling_rate, "net": net, "last": last, "avg": avg, "cost": cost,
               "margin": margin, "markup": flt((net - cost) * 100 / cost) if cost and net else None,
               "status": status, "suggested": suggested,
               "sold_qty": s["qty"] if s else 0, "sold_profit": s["profit"] if s else None,
               "sold_margin": s["margin"] if s else None}
        if not status_filter or status_filter == status:
            rows.append(row)
    rank = {"loss": 0, "low": 1, "unknown": 2, "ok": 3}
    rows.sort(key=lambda r: (rank[r["status"]], r["margin"] if r["margin"] is not None else 0))
    if request.GET.get("format") == "csv":
        # الأعمدة بأسماء حقول الاستيراد: يمكن تعديل «سعر البيع» ثم استيراد الملف بوضع «تحديث الموجود»
        return _csv("pricing.csv", ["كود الصنف", "اسم الصنف", "سعر البيع", "سعر البيع الحالي", "آخر سعر شراء",
                                    "متوسط التكلفة", "هامش الربح %", "الحالة"],
                    [[r["item"].code, r["item"].name, r["suggested"] or r["selling"], r["selling"], r["last"] or "",
                      r["avg"] or "", r["margin"] if r["margin"] is not None else "", STATUS_LABELS[r["status"]]]
                     for r in rows])
    return render(request, "reports/pricing.html", {
        "title": "تحليل التسعير والربحية", "rows": rows, "counts": counts, "target": target, "days": days,
        "item_groups": ItemGroup.objects.order_by("name"), "group": group, "status_filter": status_filter, "q": q,
        "includes_tax": prices_include_tax_default(), "status_labels": STATUS_LABELS, "query": request.GET.urlencode(),
    })


STATUS_LABELS = {"loss": "يُباع بخسارة", "low": "هامش منخفض", "ok": "جيد", "unknown": "بلا تكلفة أو سعر"}


@login_required
def price_history(request):
    from stock.models import ItemPriceHistory

    from_date, to_date = _period(request)
    entries = (ItemPriceHistory.objects.filter(created_at__date__gte=from_date, created_at__date__lte=to_date)
               .select_related("item", "user"))
    selected = {k: request.GET.get(k, "") for k in ("item", "source", "field")}
    if selected["item"]:
        entries = entries.filter(item_id=selected["item"])
    if selected["source"]:
        entries = entries.filter(source=selected["source"])
    if selected["field"]:
        entries = entries.filter(field=selected["field"])
    return render(request, "reports/price_history.html", {
        "title": "سجل تغيّر الأسعار", "from_date": from_date, "to_date": to_date, "entries": entries[:1000],
        "items": Item.objects.order_by("code"), "selected": selected, "sources": ItemPriceHistory.SOURCES,
        "fields": ItemPriceHistory.FIELDS,
    })


@login_required
def batch_report(request):
    """أرصدة التشغيلات وتواريخ صلاحيتها في كل مستودع."""
    import datetime

    from django.utils import timezone

    from stock.batches import expiry_status
    from stock.models import BatchBin

    try:
        warn_days = max(1, min(int(request.GET.get("days") or 30), 3650))
    except ValueError:
        warn_days = 30
    selected = {k: request.GET.get(k, "") for k in ("warehouse", "item", "status")}
    bins = (BatchBin.objects.filter(qty__gt=0).select_related("batch", "batch__item", "batch__item__uom", "warehouse")
            .order_by("batch__expiry_date", "batch__item__code"))
    if selected["warehouse"]:
        bins = bins.filter(warehouse_id=selected["warehouse"])
    if selected["item"]:
        bins = bins.filter(batch__item_id=selected["item"])
    valuation = {(b.item_id, b.warehouse_id): b.valuation_rate for b in Bin.objects.all()}
    today = timezone.localdate()
    rows, counts, values = [], {"expired": 0, "near": 0, "ok": 0, "none": 0}, {"expired": 0, "near": 0}
    for bb in bins:
        status = expiry_status(bb.batch, warn_days, today)
        value = bb.qty * valuation.get((bb.batch.item_id, bb.warehouse_id), 0)
        counts[status] += 1
        if status in values:
            values[status] += value
        if selected["status"] and selected["status"] != status:
            continue
        rows.append({"bb": bb, "status": status, "value": value, "days": bb.batch.days_left})
    return render(request, "reports/batches.html", {
        "title": "التشغيلات والصلاحية", "rows": rows, "counts": counts, "values": values, "warn_days": warn_days,
        "selected": selected, "warehouses": Warehouse.objects.all(),
        "items": Item.objects.filter(has_batch_no=True).order_by("code"), "today": today,
        "horizon": today + datetime.timedelta(days=warn_days),
    })


@login_required
def reorder_report(request):
    from decimal import InvalidOperation

    from django.contrib import messages
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.shortcuts import redirect

    from buying.models import Supplier
    from manufacturing.planning import create_purchase_orders
    from stock.reorder import reorder_rows

    all_wh = list(Warehouse.objects.filter(disabled=False))
    selected = request.POST.get("warehouse") or request.GET.get("warehouse") or ""
    warehouses = [w for w in all_wh if str(w.pk) == selected] or all_wh
    show_all = (request.POST.get("all") or request.GET.get("all")) == "1"
    try:
        lead = max(1, min(int(request.GET.get("lead") or 7), 365))
    except ValueError:
        lead = 7
    rows = reorder_rows(warehouses, only_below=not show_all, lead_days=lead)
    if request.method == "POST":
        picked = set(request.POST.getlist("pick"))
        suppliers = Supplier.objects.in_bulk()
        try:
            lines = []
            for r in rows:
                key = str(r["item"].pk)
                if key not in picked:
                    continue
                supplier = suppliers.get(int(request.POST.get(f"supplier_{key}") or 0))
                if supplier is None:
                    raise ValidationError(f"اختر المورد للصنف «{r['item'].name}».")
                qty = Decimal(request.POST.get(f"qty_{key}") or r["suggested"])
                if qty > 0:
                    lines.append((r["item"], supplier, qty, r["rate"]))
            if not lines:
                raise ValidationError("حدد صنفاً واحداً على الأقل.")
            with transaction.atomic():
                created = create_purchase_orders(lines, warehouses[0] if len(warehouses) == 1 else all_wh[0],
                                                 request.user)
            messages.success(request, "أُنشئت أوامر شراء (مسودات): " + "، ".join(p.name for p in created))
            return redirect("doc_list", slug="purchase-order")
        except (ValidationError, InvalidOperation) as exc:
            messages.error(request, "؛ ".join(exc.messages) if isinstance(exc, ValidationError) else "كمية غير صالحة.")
    below = [r for r in rows if r["below"]]
    return render(request, "reports/reorder.html", {
        "title": "حد إعادة الطلب", "rows": rows, "warehouses": all_wh, "selected": selected, "show_all": show_all,
        "lead": lead, "count": len(below), "amount": sum((r["amount"] for r in below), Decimal(0)),
        "suppliers": Supplier.objects.filter(disabled=False).order_by("name"),
    })
