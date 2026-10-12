"""دفتر حركة المخزون والتقييم بطريقة المتوسط المرجح المتحرك."""
from decimal import Decimal

from django.core.exceptions import ValidationError

from core.utils import flt


def _locked_bin(item, warehouse):
    from stock.models import Bin

    Bin.all_objects.get_or_create(item=item, warehouse=warehouse, defaults={"company": warehouse.company})
    return Bin.all_objects.select_for_update().get(item=item, warehouse=warehouse)


def make_stock_ledger_entry(doc, item, warehouse, qty, incoming_rate=None, batch_no="", expiry_date=None,
                            manufacturing_date=None, allocations=None, serial_nos=None):
    """تسجيل حركة مخزون: الكمية الموجبة وارد والسالبة صادر. تعيد فرق قيمة المخزون.

    للأصناف المتتبعة بالتشغيلات تُوزَّع الكمية على تشغيلة أو أكثر (سطر حركة لكل تشغيلة).
    allocations: قائمة تُضاف إليها (التشغيلة، الكمية، فرق القيمة) لكل سطر، لمن يحتاج تتبع التوزيع (التحويل).
    """
    if not item.is_stock_item:
        return flt(0)
    qty = flt(qty, 3)
    if not item.has_batch_no or not qty:
        diff = _post(doc, item, warehouse, qty, incoming_rate, None)
        if qty:
            from stock import serials

            serials.apply(doc, item, warehouse, qty, serial_nos, doc._last_sle)
        if allocations is not None:
            allocations.append((None, qty, diff))
        return diff
    from stock.batches import locked_batch_bin, plan_issue, plan_receipt

    if qty > 0:
        plan = plan_receipt(doc, item, qty, batch_no, expiry_date, manufacturing_date)
    else:
        plan = [(b, -q) for b, q in plan_issue(doc, item, warehouse, -qty, batch_no)]
    total = flt(0)
    for batch, part in plan:
        bb = locked_batch_bin(batch, warehouse)
        if flt(bb.qty + part, 3) < 0:
            raise ValidationError(f"رصيد التشغيلة {batch.batch_no} من «{item.name}» في «{warehouse}» "
                                  f"هو {bb.qty} والمطلوب {-part}.")
        bb.qty = flt(bb.qty + part, 3)
        bb.save()
        diff = _post(doc, item, warehouse, part, incoming_rate, batch)
        total += diff
        if allocations is not None:
            allocations.append((batch, part, diff))
    return total


def fifo_seed(b, qty, value):
    """طبقات الرصيد؛ الرصيد السابق لتفعيل FIFO (أو المختلف عن الطبقات) يصبح طبقة واحدة بمتوسطه."""
    queue = [[Decimal(q), Decimal(r)] for q, r in (b.fifo_queue or [])]
    if flt(sum((q for q, _r in queue), Decimal(0)), 3) != flt(qty, 3):
        queue = [[Decimal(qty), Decimal(value) / Decimal(qty)]] if qty > 0 else []
    return [[str(q), str(r)] for q, r in queue]


def fifo_consume(queue, qty):
    """يصرف الكمية من أقدم الطبقات؛ يعيد (الطبقات المتبقية، تكلفة المصروف)."""
    remaining, cost, result = Decimal(qty), Decimal(0), []
    for q, r in ([Decimal(q), Decimal(r)] for q, r in queue):
        take = min(q, remaining)
        cost += take * r
        remaining -= take
        if q - take > 0:
            result.append([str(q - take), str(r)])
    return result, flt(cost)


def rebuild_fifo(item, warehouse):
    """بعد الإلغاء: إعادة بناء الطبقات من الحركات النشطة بالترتيب (الإلغاء قد يكون من منتصف السجل)."""
    from stock.models import StockLedgerEntry

    b = _locked_bin(item, warehouse)
    queue = []
    for sle in StockLedgerEntry.objects.filter(item=item, warehouse=warehouse, is_cancelled=False).order_by("id"):
        if sle.actual_qty > 0:
            queue.append([str(sle.actual_qty), str(sle.incoming_rate)])
        elif sle.actual_qty < 0:
            queue, _cost = fifo_consume(queue, -sle.actual_qty)
        elif sle.stock_value_difference and queue:
            queue = fifo_spread(queue, sle.stock_value_difference)
    b.fifo_queue = queue
    b.save(update_fields=["fifo_queue"])


def fifo_spread(queue, value):
    """توزيع قيمة إضافية (تكاليف شحن) على الطبقات بنسبة كمياتها."""
    total = sum((Decimal(q) for q, _r in queue), Decimal(0))
    if not total:
        return queue
    per_unit = Decimal(value) / total
    return [[q, str(Decimal(r) + per_unit)] for q, r in queue]


def _post(doc, item, warehouse, qty, incoming_rate, batch):
    from stock.models import StockLedgerEntry

    b = _locked_bin(item, warehouse)
    old_qty, old_value = flt(b.actual_qty, 3), flt(b.stock_value)
    new_qty = old_qty + qty

    if new_qty < 0:
        raise ValidationError(
            f"الرصيد غير كافٍ: المتاح من «{item.name}» في «{warehouse}» هو {old_qty} والمطلوب {-qty}."
        )
    if qty > 0:
        rate = Decimal(str(incoming_rate)) if incoming_rate is not None else b.valuation_rate
        new_value = flt(old_value + qty * rate)
        b.valuation_rate = flt(new_value / new_qty, 6) if new_qty else flt(rate, 6)
        if item.valuation_method == "fifo":
            b.fifo_queue = fifo_seed(b, old_qty, old_value) + [[str(qty), str(rate)]]
    else:
        rate = Decimal(0)
        if item.valuation_method == "fifo" and qty:
            queue, consumed = fifo_consume(fifo_seed(b, old_qty, old_value), -qty)
            b.fifo_queue = queue
            new_value = flt(old_value - consumed) if new_qty else flt(0)
            b.valuation_rate = flt(new_value / new_qty, 6) if new_qty else b.valuation_rate
        else:
            new_value = flt(new_qty * b.valuation_rate) if new_qty else flt(0)

    difference = new_value - old_value
    b.actual_qty, b.stock_value = new_qty, new_value
    b.save()

    doc._last_sle = StockLedgerEntry.objects.create(
        company=doc.company,
        posting_date=doc.posting_date,
        item=item,
        warehouse=warehouse,
        actual_qty=qty,
        incoming_rate=rate,
        valuation_rate=b.valuation_rate,
        qty_after_transaction=new_qty,
        stock_value_difference=difference,
        voucher_type=doc.doctype_slug,
        voucher_no=doc.name,
        voucher_id=doc.pk,
        batch=batch,
    )
    return difference


def make_value_adjustment(doc, item, warehouse, value):
    """زيادة (أو نقص) قيمة رصيد صنف في مستودع دون تغيير كميته — مثل تحميل تكاليف الشحن والجمارك.

    تعيد القيمة المضافة فعلاً (صفر إن لم يكن للصنف رصيد في المستودع).
    """
    from stock.models import StockLedgerEntry

    value = flt(value)
    b = _locked_bin(item, warehouse)
    if not value or flt(b.actual_qty, 3) <= 0:
        return flt(0)
    b.stock_value = flt(b.stock_value + value)
    if item.valuation_method == "fifo":
        b.fifo_queue = fifo_spread(fifo_seed(b, b.actual_qty, b.stock_value - value), value)
    if b.stock_value < 0:
        raise ValidationError(f"لا يمكن أن تصبح قيمة رصيد «{item.name}» في «{warehouse}» سالبة.")
    b.valuation_rate = flt(b.stock_value / b.actual_qty, 6)
    b.save()
    StockLedgerEntry.objects.create(
        company=doc.company, posting_date=doc.posting_date, item=item, warehouse=warehouse, actual_qty=0,
        incoming_rate=0, valuation_rate=b.valuation_rate, qty_after_transaction=b.actual_qty,
        stock_value_difference=value, voucher_type=doc.doctype_slug, voucher_no=doc.name, voucher_id=doc.pk,
    )
    return value


def cancel_stock_ledger_entries(doc):
    """عكس أثر حركات المستند على الأرصدة ووسمها كملغاة."""
    from stock.models import StockLedgerEntry

    entries = StockLedgerEntry.all_objects.filter(
        voucher_type=doc.doctype_slug, voucher_id=doc.pk, is_cancelled=False
    ).select_related("item", "warehouse", "batch").order_by("-id")
    for sle in entries:
        if sle.batch_id:
            from stock.batches import locked_batch_bin

            bb = locked_batch_bin(sle.batch, sle.warehouse)
            bb.qty = flt(bb.qty - sle.actual_qty, 3)
            if bb.qty < 0:
                raise ValidationError(f"لا يمكن الإلغاء: سيصبح رصيد التشغيلة {sle.batch.batch_no} "
                                      f"من «{sle.item.name}» سالباً.")
            bb.save()
        b = _locked_bin(sle.item, sle.warehouse)
        b.actual_qty = flt(b.actual_qty - sle.actual_qty, 3)
        if b.actual_qty < 0:
            raise ValidationError(
                f"لا يمكن الإلغاء: سيصبح رصيد «{sle.item.name}» في «{sle.warehouse}» سالباً."
            )
        b.stock_value = flt(b.stock_value - sle.stock_value_difference) if b.actual_qty else flt(0)
        if b.actual_qty:
            b.valuation_rate = flt(b.stock_value / b.actual_qty, 6)
        b.save()
    ids = list(entries.values_list("id", flat=True))
    touched = {(sle.item, sle.warehouse) for sle in entries if sle.item.valuation_method == "fifo"}
    entries.update(is_cancelled=True)
    for item, warehouse in touched:
        rebuild_fifo(item, warehouse)
    from stock import serials

    serials.restore(StockLedgerEntry.all_objects.filter(id__in=ids))
