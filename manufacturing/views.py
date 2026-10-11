from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render

from accounts.reports import _period
from core.utils import flt
from manufacturing.planning import create_productions, create_purchase_orders, plan


def _warehouses(request):
    from stock.models import Warehouse

    all_wh = list(Warehouse.objects.filter(disabled=False))
    selected = request.POST.get("warehouse") or request.GET.get("warehouse") or ""
    chosen = [w for w in all_wh if str(w.pk) == selected] or all_wh
    return all_wh, chosen, selected


@login_required
def mrp(request):
    from buying.models import Supplier

    all_wh, warehouses, selected = _warehouses(request)
    flags = request.POST if request.method == "POST" else request.GET
    first_visit = request.method == "GET" and "run" not in request.GET
    include_productions = first_visit or bool(flags.get("productions"))
    include_sales_orders = first_visit or bool(flags.get("sales_orders"))
    rows = plan(warehouses, include_productions, include_sales_orders)
    if request.method == "POST":
        target = warehouses[0] if len(warehouses) == 1 else (all_wh[0] if all_wh else None)
        picked = set(request.POST.getlist("pick"))
        try:
            with transaction.atomic():
                if request.POST.get("action") == "purchase":
                    suppliers = Supplier.objects.in_bulk()
                    lines = []
                    for r in rows:
                        key = str(r["item"].pk)
                        if r["action"] != "buy" or key not in picked:
                            continue
                        supplier = suppliers.get(int(request.POST.get(f"supplier_{key}") or 0))
                        if supplier is None:
                            raise ValidationError(f"اختر المورد للصنف «{r['item'].name}».")
                        qty = Decimal(request.POST.get(f"qty_{key}") or r["shortage"])
                        if qty > 0:
                            lines.append((r["item"], supplier, qty, r["rate"]))
                    if not lines:
                        raise ValidationError("حدد صنفاً واحداً على الأقل للشراء.")
                    created = create_purchase_orders(lines, target, request.user)
                    messages.success(request, "أُنشئت أوامر شراء (مسودات): " + "، ".join(p.name for p in created))
                    return redirect("doc_list", slug="purchase-order")
                lines = [(r["bom"], Decimal(request.POST.get(f"qty_{r['item'].pk}") or r["shortage"]))
                         for r in rows if r["action"] == "produce" and str(r["item"].pk) in picked]
                lines = [(b, q) for b, q in lines if q > 0]
                if not lines:
                    raise ValidationError("حدد منتجاً واحداً على الأقل للإنتاج.")
                created = create_productions(lines, target, request.user)
                messages.success(request, "أُنشئت أوامر إنتاج (مسودات): " + "، ".join(p.name for p in created))
                return redirect("doc_list", slug="production")
        except (ValidationError, InvalidOperation) as exc:
            messages.error(request, "؛ ".join(exc.messages) if isinstance(exc, ValidationError) else "كمية غير صالحة.")
    totals = {"buy": sum((r.get("amount") or 0 for r in rows if r["action"] == "buy"), flt(0)),
              "buy_count": sum(1 for r in rows if r["action"] == "buy"),
              "produce_count": sum(1 for r in rows if r["action"] == "produce")}
    return render(request, "manufacturing/mrp.html", {
        "title": "تخطيط احتياجات المواد", "rows": rows, "totals": totals, "warehouses": all_wh, "selected": selected,
        "include_productions": include_productions, "include_sales_orders": include_sales_orders,
        "suppliers": Supplier.objects.filter(disabled=False).order_by("name"),
    })


@login_required
def production_report(request):
    """الإنتاج والهالك: الكميات والتالف وتكلفة الوحدة وانحراف استهلاك المواد عن قائمة المواد، وتحميل محطات العمل."""
    from collections import defaultdict

    from manufacturing.models import Production

    from_date, to_date = _period(request)
    productions = list(Production.objects.filter(docstatus=1, posting_date__gte=from_date, posting_date__lte=to_date)
                       .select_related("item", "item__uom", "bom", "bom__routing").order_by("-posting_date", "-id"))
    by_item = defaultdict(lambda: {"orders": 0, "qty": flt(0, 3), "rejected": flt(0, 3), "material": flt(0),
                                   "operating": flt(0), "scrap": flt(0), "total": flt(0)})
    stations = defaultdict(lambda: {"minutes": flt(0, 3), "cost": flt(0)})
    details = []
    for p in productions:
        g = by_item[p.item]
        g["orders"] += 1
        g["qty"] += p.qty
        g["rejected"] += p.rejected_qty
        g["material"] += p.material_cost
        g["operating"] += p.operating_cost + p.additional_cost
        g["scrap"] += p.scrap_value
        g["total"] += p.total_cost
        # الانحراف: تكلفة المواد الفعلية مقابل تكلفة الكميات القياسية في القائمة بنفس أسعار الصرف الفعلية
        standard = flt(0)
        for row in p.items.all():
            standard += flt(row.required_qty * row.rate)
        details.append({"p": p, "standard": standard, "variance": flt(p.material_cost - standard)})
        if p.bom.routing_id:
            for op in p.bom.routing.operations.select_related("workstation"):
                minutes = flt(op.minutes * p.qty, 3)
                stations[op.workstation]["minutes"] += minutes
                stations[op.workstation]["cost"] += flt(minutes * op.workstation.hourly_rate / 60)
    summary = []
    for item, g in by_item.items():
        good = g["qty"] - g["rejected"]
        summary.append(dict(g, item=item, good=good, unit=flt(g["total"] / good, 4) if good else None,
                            waste=flt(g["rejected"] * 100 / g["qty"]) if g["qty"] else 0))
    summary.sort(key=lambda r: -r["total"])
    station_rows = sorted(({"ws": ws, "hours": flt(v["minutes"] / 60, 2), "cost": v["cost"]} for ws, v in stations.items()),
                          key=lambda r: -r["hours"])
    return render(request, "manufacturing/report.html", {
        "title": "تقرير الإنتاج والهالك", "from_date": from_date, "to_date": to_date, "summary": summary,
        "details": details, "stations": station_rows,
    })
