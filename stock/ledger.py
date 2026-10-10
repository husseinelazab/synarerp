"""دفتر حركة المخزون والتقييم بطريقة المتوسط المرجح المتحرك."""
from decimal import Decimal

from django.core.exceptions import ValidationError

from core.utils import flt


def _locked_bin(item, warehouse):
    from stock.models import Bin

    Bin.objects.get_or_create(item=item, warehouse=warehouse)
    return Bin.objects.select_for_update().get(item=item, warehouse=warehouse)


def make_stock_ledger_entry(doc, item, warehouse, qty, incoming_rate=None):
    """تسجيل حركة مخزون: الكمية الموجبة وارد والسالبة صادر. تعيد فرق قيمة المخزون."""
    from stock.models import StockLedgerEntry

    if not item.is_stock_item:
        return flt(0)
    qty = flt(qty, 3)
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
    )
    return difference


def cancel_stock_ledger_entries(doc):
    """عكس أثر حركات المستند على الأرصدة ووسمها كملغاة."""
    from stock.models import StockLedgerEntry

    entries = StockLedgerEntry.objects.filter(
        voucher_type=doc.doctype_slug, voucher_no=doc.name, is_cancelled=False
    ).select_related("item", "warehouse").order_by("-id")
    for sle in entries:
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
