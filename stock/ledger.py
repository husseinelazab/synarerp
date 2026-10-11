"""دفتر حركة المخزون والتقييم بطريقة المتوسط المرجح المتحرك."""
from decimal import Decimal

from django.core.exceptions import ValidationError

from core.utils import flt


def _locked_bin(item, warehouse):
    from stock.models import Bin

    Bin.all_objects.get_or_create(item=item, warehouse=warehouse, defaults={"company": warehouse.company})
    return Bin.all_objects.select_for_update().get(item=item, warehouse=warehouse)


def make_stock_ledger_entry(doc, item, warehouse, qty, incoming_rate=None, batch_no="", expiry_date=None,
                            manufacturing_date=None, allocations=None):
    """تسجيل حركة مخزون: الكمية الموجبة وارد والسالبة صادر. تعيد فرق قيمة المخزون.

    للأصناف المتتبعة بالتشغيلات تُوزَّع الكمية على تشغيلة أو أكثر (سطر حركة لكل تشغيلة).
    allocations: قائمة تُضاف إليها (التشغيلة، الكمية، فرق القيمة) لكل سطر، لمن يحتاج تتبع التوزيع (التحويل).
    """
    if not item.is_stock_item:
        return flt(0)
    qty = flt(qty, 3)
    if not item.has_batch_no or not qty:
        diff = _post(doc, item, warehouse, qty, incoming_rate, None)
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


def _post(doc, item, warehouse, qty, incoming_rate, batch):
    from stock.models import StockLedgerEntry

    b = _locked_bin(item, warehouse)
    old_qty, old_value = flt(b.actual_qty, 3), flt(b.stock_value)
    new_qty = old_qty + qty

    if qty > 0:
        rate = Decimal(str(incoming_rate)) if incoming_rate is not None else b.valuation_rate
        new_value = flt(old_value + qty * rate)
        b.valuation_rate = flt(new_value / new_qty, 6) if new_qty else flt(rate, 6)
    else:
        if new_qty < 0:
            raise ValidationError(
                f"الرصيد غير كافٍ: المتاح من «{item.name}» في «{warehouse}» هو {old_qty} والمطلوب {-qty}."
            )
        rate = Decimal(0)
        new_value = flt(new_qty * b.valuation_rate) if new_qty else flt(0)

    difference = new_value - old_value
    b.actual_qty, b.stock_value = new_qty, new_value
    b.save()

    StockLedgerEntry.objects.create(
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
    entries.update(is_cancelled=True)


def adjust_stock_value(doc, item, warehouse, amount):
    """إضافة قيمة لرصيد صنف دون تغيير كميته (تكاليف الشحن والجمارك المحمّلة). تعيد ما أُضيف فعلاً للمخزون."""
    from stock.models import StockLedgerEntry

    amount = flt(amount)
    b = _locked_bin(item, warehouse)
    if not amount or b.actual_qty <= 0:
        return flt(0)
    b.stock_value = flt(b.stock_value + amount)
    b.valuation_rate = flt(b.stock_value / b.actual_qty, 6)
    b.save()
    StockLedgerEntry.objects.create(
        company=doc.company, posting_date=doc.posting_date, item=item, warehouse=warehouse, actual_qty=0,
        incoming_rate=0, valuation_rate=b.valuation_rate, qty_after_transaction=b.actual_qty,
        stock_value_difference=amount, voucher_type=doc.doctype_slug, voucher_no=doc.name, voucher_id=doc.pk,
    )
    return amount
