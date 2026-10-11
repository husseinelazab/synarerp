"""تخطيط احتياجات المواد (MRP).

الطلب: أوامر الإنتاج المسودة (موادها)، وأوامر البيع المفتوحة لمنتجات لها قائمة مواد (ما لم يُسلَّم بعد
مطروحاً منه رصيد المنتج وما هو مخطط إنتاجه). يُفكك الطلب عبر قوائم المواد متعددة المستويات مع صافي
الرصيد: المكوّن نصف المصنّع الناقص يصبح طلب إنتاج تُفكك مواده بدورها.
"""
from collections import OrderedDict, defaultdict
from decimal import Decimal

from django.db.models import Sum

from core.utils import flt


def stock_available(item, warehouses):
    from django.utils import timezone

    from stock.batches import available_batches
    from stock.models import Bin

    if item.has_batch_no:
        return sum((q for wh in warehouses for _b, q in available_batches(item, wh, timezone.localdate())),
                   flt(0, 3))
    return flt(Bin.objects.filter(item=item, warehouse__in=warehouses).aggregate(q=Sum("actual_qty"))["q"] or 0, 3)


def open_purchase_qty():
    """الكميات المطلوبة من الموردين ولم تُستلم بعد (أوامر شراء معتمدة مفتوحة)."""
    from buying.models import PurchaseOrderItem
    from core.commercial import CommercialStatus

    result = defaultdict(Decimal)
    rows = (PurchaseOrderItem.objects.filter(parent__docstatus=1, parent__status__in=[
        CommercialStatus.TO_BILL, CommercialStatus.PARTLY_BILLED]).values("item_id", "qty", "delivered_qty", "billed_qty"))
    for r in rows:
        remaining = r["qty"] - max(r["delivered_qty"], r["billed_qty"])
        if remaining > 0:
            result[r["item_id"]] += remaining
    return result


def last_supplier(item):
    from buying.models import PurchaseInvoiceItem, PurchaseOrderItem

    for model in (PurchaseInvoiceItem, PurchaseOrderItem):
        row = (model.objects.filter(item=item, parent__docstatus=1).select_related("parent__supplier")
               .order_by("-parent__posting_date", "-id").first())
        if row is not None:
            return row.parent.supplier
    return None


def plan(warehouses, include_productions=True, include_sales_orders=True):
    from manufacturing.models import Production, default_bom
    from selling.models import SalesOrderItem
    from stock.models import Item
    from core.commercial import CommercialStatus

    required = defaultdict(Decimal)        # احتياج المواد الإجمالي
    sources = defaultdict(set)             # من أين جاء الاحتياج
    to_produce = OrderedDict()             # منتجات/مكونات يلزم إنتاجها: item_id → (bom، الكمية)
    planned_output = defaultdict(Decimal)  # ما هو مخطط إنتاجه أصلاً في مسودات أوامر الإنتاج

    drafts = Production.objects.filter(docstatus=0).select_related("bom") if include_productions else []
    for p in drafts:
        planned_output[p.item_id] += p.qty
        for row in p.items.all():
            required[row.item_id] += row.qty
            sources[row.item_id].add(p.name)

    if include_sales_orders:
        demand = defaultdict(Decimal)
        so_names = defaultdict(set)
        rows = (SalesOrderItem.objects.filter(parent__docstatus=1, parent__status__in=[
            CommercialStatus.TO_BILL, CommercialStatus.PARTLY_BILLED]).select_related("parent", "item"))
        for r in rows:
            remaining = r.qty - max(r.delivered_qty, r.billed_qty)
            if remaining > 0 and default_bom(r.item) is not None:
                demand[r.item_id] += remaining
                so_names[r.item_id].add(r.parent.name)
        items = Item.objects.in_bulk(list(demand))
        for item_id, qty in demand.items():
            net = flt(qty - stock_available(items[item_id], warehouses) - planned_output[item_id], 3)
            if net > 0:
                to_produce[item_id] = [default_bom(items[item_id]), net, so_names[item_id]]

    # تفكيك متعدد المستويات مع صافي الرصيد لكل مستوى
    exploded = set()
    for _level in range(10):
        for item_id, (bom, qty, refs) in list(to_produce.items()):
            if item_id in exploded:
                continue
            exploded.add(item_id)
            for mat, q in bom.scaled(qty):
                required[mat.pk] += q
                sources[mat.pk].add(f"{bom.item.name}")
        items = Item.objects.in_bulk(list(required))
        changed = False
        for item_id, qty in required.items():
            if item_id in to_produce:
                continue
            bom = default_bom(items[item_id])
            if bom is None:
                continue
            net = flt(qty - stock_available(items[item_id], warehouses) - planned_output[item_id], 3)
            if net > 0:
                to_produce[item_id] = [bom, net, sources[item_id]]
                changed = True
        if not changed and all(i in exploded for i in to_produce):
            break

    on_order = open_purchase_qty()
    items = Item.objects.in_bulk(list(set(required) | set(to_produce))).values()
    rows = []
    for item in sorted(items, key=lambda i: i.code):
        need = required.get(item.pk, Decimal(0))
        if item.pk in to_produce:
            bom, qty, refs = to_produce[item.pk]
            available = stock_available(item, warehouses)
            rows.append({"item": item, "required": max(need, qty), "available": available, "on_order": Decimal(0),
                         "shortage": qty, "action": "produce", "bom": bom, "refs": sorted(refs)})
            continue
        if not need:
            continue
        available = stock_available(item, warehouses)
        ordered = flt(on_order.get(item.pk, 0), 3)
        shortage = max(flt(need - available - ordered, 3), flt(0, 3))
        supplier = last_supplier(item) if shortage else None
        rows.append({"item": item, "required": flt(need, 3), "available": available, "on_order": ordered,
                     "shortage": shortage, "action": "buy" if shortage else "ok", "supplier": supplier,
                     "rate": item.buying_rate, "amount": flt(shortage * item.buying_rate),
                     "refs": sorted(sources[item.pk])})
    order = {"produce": 0, "buy": 1, "ok": 2}
    rows.sort(key=lambda r: (order[r["action"]], r["item"].code))
    return rows


def create_purchase_orders(lines, warehouse, user=None):
    """lines: [(صنف، مورد، كمية، سعر)] → أوامر شراء مسودة، واحد لكل مورد."""
    from buying.models import PurchaseOrder

    by_supplier = OrderedDict()
    for item, supplier, qty, rate in lines:
        by_supplier.setdefault(supplier, []).append((item, qty, rate))
    created = []
    for supplier, rows in by_supplier.items():
        po = PurchaseOrder.objects.create(company=supplier.company, supplier=supplier, warehouse=warehouse,
                                          created_by=user, remarks="من تخطيط احتياجات المواد")
        for item, qty, rate in rows:
            po.items.create(item=item, qty=qty, rate=rate)
        po.calculate_totals()
        po.save()
        created.append(po)
    return created


def create_productions(lines, warehouse, user=None):
    from manufacturing.models import Production

    created = []
    for bom, qty in lines:
        p = Production.objects.create(company=bom.company, bom=bom, qty=qty, source_warehouse=warehouse,
                                      target_warehouse=warehouse, created_by=user,
                                      remarks="من تخطيط احتياجات المواد")
        p.calculate_totals()
        p.save()
        created.append(p)
    return created
