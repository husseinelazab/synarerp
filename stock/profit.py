"""الربحية: صافي المبيعات مقابل تكلفتها الفعلية من حركة المخزون، على مستوى السطر ثم بأي تجميع."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

from core.utils import flt

GROUPINGS = {
    "item": "الصنف",
    "item_group": "مجموعة الأصناف",
    "customer": "العميل",
    "invoice": "الفاتورة",
    "cost_center": "الفرع / مركز التكلفة",
    "month": "الشهر",
}


def _stock_costs(voucher_type, ids):
    """تكلفة المخزون المصروفة لكل (مستند، صنف) من دفتر حركة المخزون (موجبة للبيع، سالبة للمرتجع)."""
    from stock.models import StockLedgerEntry

    if not ids:
        return {}
    rows = (StockLedgerEntry.objects.filter(voucher_type=voucher_type, voucher_id__in=ids, is_cancelled=False)
            .values("voucher_id", "item_id").annotate(v=Sum("stock_value_difference"), q=Sum("actual_qty")))
    return {(r["voucher_id"], r["item_id"]): (-flt(r["v"]), -flt(r["q"], 3)) for r in rows}


def sales_lines(from_date, to_date, item_group=None, customer=None, cost_center=None, item=None):
    """أسطر المبيعات المعتمدة في الفترة مع صافي البيع وتكلفته بعملة الشركة."""
    from selling.models import SalesInvoiceItem
    from stock.models import Bin

    qs = (SalesInvoiceItem.objects.filter(parent__docstatus=1, parent__posting_date__gte=from_date,
                                          parent__posting_date__lte=to_date)
          .select_related("parent", "parent__customer", "parent__cost_center", "item", "item__item_group",
                          "delivery_note_item"))
    if item_group:
        qs = qs.filter(item__item_group_id=item_group)
    if customer:
        qs = qs.filter(parent__customer_id=customer)
    if cost_center:
        qs = qs.filter(parent__cost_center_id=cost_center)
    if item:
        qs = qs.filter(item_id=item)
    rows = list(qs.order_by("parent__posting_date", "parent_id", "id"))
    parent_ids = {r.parent_id for r in rows}
    from selling.models import SalesInvoiceItem as SII

    parent_amounts = {p["parent_id"]: flt(p["t"]) for p in SII.objects.filter(parent_id__in=parent_ids)
                      .values("parent_id").annotate(t=Sum("amount"))}
    invoice_costs = _stock_costs("sales-invoice", parent_ids)
    dn_ids = {r.delivery_note_item.parent_id for r in rows if r.delivery_note_item_id}
    dn_costs = _stock_costs("delivery-note", dn_ids)
    # توزيع تكلفة الصنف في الفاتورة على أسطره بنسبة الكميات
    qty_per_key = defaultdict(Decimal)
    for r in rows:
        qty_per_key[(r.parent_id, r.item_id)] += abs(r.stock_qty)
    avg_cost = {}
    lines = []
    for r in rows:
        inv = r.parent
        base_net = inv.base_net_total if inv.base_net_total or not inv.net_total else inv.net_total
        total = parent_amounts.get(inv.pk) or Decimal(0)
        net = flt(base_net * r.amount / total) if total else flt(0)
        estimated = False
        if not r.item.is_stock_item:
            cost = flt(0)
        elif (inv.pk, r.item_id) in invoice_costs:
            value, _qty = invoice_costs[(inv.pk, r.item_id)]
            share = abs(r.stock_qty) / qty_per_key[(inv.pk, r.item_id)] if qty_per_key[(inv.pk, r.item_id)] else 0
            cost = flt(value * share)
        elif r.delivery_note_item_id and (r.delivery_note_item.parent_id, r.item_id) in dn_costs:
            value, qty = dn_costs[(r.delivery_note_item.parent_id, r.item_id)]
            cost = flt(value / qty * r.stock_qty) if qty else flt(0)
        else:
            # لا حركة مخزون لهذا السطر (فاتورة بدون تحديث مخزون): تقدير بمتوسط التكلفة الحالي
            if r.item_id not in avg_cost:
                agg = Bin.objects.filter(item_id=r.item_id).aggregate(v=Sum("stock_value"), q=Sum("actual_qty"))
                avg_cost[r.item_id] = (flt(agg["v"]) / flt(agg["q"], 3)) if agg["q"] else flt(r.item.buying_rate)
            cost = flt(avg_cost[r.item_id] * r.stock_qty)
            estimated = True
        lines.append({"line": r, "invoice": inv, "qty": flt(r.stock_qty, 3), "sales": net, "cost": cost,
                      "profit": net - cost, "estimated": estimated})
    return lines


def _key(line, group_by):
    r, inv = line["line"], line["invoice"]
    if group_by == "item_group":
        return r.item.item_group_id, str(r.item.item_group), None
    if group_by == "customer":
        return inv.customer_id, str(inv.customer), inv.customer.get_absolute_url() if hasattr(inv.customer, "get_absolute_url") else None
    if group_by == "invoice":
        return inv.pk, f"{inv.name} — {inv.posting_date:%Y-%m-%d} — {inv.customer}", inv.get_absolute_url()
    if group_by == "cost_center":
        return inv.cost_center_id, str(inv.cost_center) if inv.cost_center_id else "بدون مركز تكلفة", None
    if group_by == "month":
        return inv.posting_date.strftime("%Y-%m"), inv.posting_date.strftime("%Y-%m"), None
    return r.item_id, str(r.item), None


def gross_profit(from_date, to_date, group_by="item", **filters):
    lines = sales_lines(from_date, to_date, **filters)
    groups = {}
    for line in lines:
        key, label, url = _key(line, group_by)
        g = groups.setdefault(key, {"key": key, "label": label, "url": url, "qty": flt(0, 3), "sales": flt(0),
                                    "cost": flt(0), "profit": flt(0), "lines": 0, "estimated": 0})
        g["qty"] += line["qty"]
        g["sales"] += line["sales"]
        g["cost"] += line["cost"]
        g["profit"] += line["profit"]
        g["lines"] += 1
        g["estimated"] += line["estimated"]
    rows = list(groups.values())
    for g in rows:
        g["margin"] = flt(g["profit"] * 100 / g["sales"]) if g["sales"] else None
        g["markup"] = flt(g["profit"] * 100 / g["cost"]) if g["cost"] else None
    totals = {k: sum((g[k] for g in rows), flt(0)) for k in ("sales", "cost", "profit")}
    totals["margin"] = flt(totals["profit"] * 100 / totals["sales"]) if totals["sales"] else None
    totals["estimated"] = sum(g["estimated"] for g in rows)
    return rows, totals
