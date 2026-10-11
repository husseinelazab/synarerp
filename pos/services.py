"""منطق البيع في نقطة البيع: إنشاء فاتورة مبيعات مبسطة واعتمادها في عملية واحدة."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.utils import flt
from selling.models import Customer, SalesInvoice
from pos.models import CardTransaction, clean_card_info
from stock.models import Item


def checkout(shift, user, lines, customer_id=None, discount=0, paid_cash=0, paid_card=0, offline_id="",
             offline_at=None, card=None):
    """lines: قائمة من {"item": id, "qty": رقم, "rate": رقم اختياري}.

    offline_id: معرّف البيع من الجهاز عند البيع دون اتصال؛ إن سبقت مزامنته تُعاد نفس الفاتورة.
    """
    if offline_id:
        existing = SalesInvoice.objects.filter(offline_id=offline_id, is_pos=True).exclude(docstatus=2).first()
        if existing is not None:
            return existing
    if not shift or not shift.is_open:
        raise ValidationError("يجب فتح وردية قبل البيع.")
    if shift.user_id != user.id:
        raise ValidationError("هذه الوردية لا تخص المستخدم الحالي.")
    if not lines:
        raise ValidationError("السلة فارغة.")
    profile = shift.profile
    customer = Customer.objects.get(pk=customer_id) if customer_id else profile.default_customer
    discount = flt(discount)

    with transaction.atomic():
        invoice = SalesInvoice.objects.create(
            company=profile.company, customer=customer, is_pos=True, pos_shift=shift, created_by=user,
            update_stock=True, warehouse=profile.warehouse, tax_rate=profile.tax_rate, cost_center=profile.cost_center,
            prices_include_tax=profile.prices_include_tax, discount_amount=discount,
            offline_id=offline_id or "", offline_at=offline_at,
            **({"posting_date": timezone.localtime(offline_at).date()} if offline_at else {}),
        )
        for line in lines:
            try:
                item = Item.objects.get(pk=line.get("item"), disabled=False)
            except (Item.DoesNotExist, ValueError, TypeError):
                raise ValidationError("صنف غير موجود أو موقوف.")
            qty = flt(line.get("qty"), 3)
            if qty <= 0:
                raise ValidationError(f"الكمية للصنف {item.name} يجب أن تكون أكبر من صفر.")
            uom_id = int(line["uom"]) if str(line.get("uom") or "").isdigit() else None
            rate = item.price_for(uom_id, "selling")  # سعر الكرتون مثلاً، أو الأساسي × المعامل
            if profile.allow_rate_change and line.get("rate") not in (None, ""):
                rate = flt(line["rate"])
            invoice.items.create(item=item, qty=qty, rate=rate, uom_id=uom_id)
        invoice.calculate_totals()

        if discount:
            if not profile.allow_discount:
                raise ValidationError("الخصم غير مسموح في نقطة البيع هذه.")
            if discount > flt(invoice.total * profile.max_discount_percent / 100):
                raise ValidationError(f"الخصم يتجاوز الحد المسموح ({profile.max_discount_percent}%).")

        invoice.paid_cash = flt(paid_cash)
        invoice.paid_card = flt(paid_card)
        invoice.change_amount = max(flt(invoice.paid_cash + invoice.paid_card - invoice.grand_total), flt(0))
        card_data = None
        if invoice.paid_card > 0:
            card_data = clean_card_info(card, profile.card_terminal_mode, invoice.paid_card,
                                        profile.require_card_reference)
        invoice.save()
        invoice.submit()
        if card_data is not None:
            CardTransaction.objects.create(shift=shift, invoice=invoice, amount=invoice.paid_card,
                                           mode=profile.card_terminal_mode, **card_data)
    return invoice


def find_returnable_invoice(name):
    try:
        return SalesInvoice.objects.get(name=name.strip(), docstatus=1, is_pos=True, is_return=False)
    except SalesInvoice.DoesNotExist:
        raise ValidationError("الفاتورة غير موجودة، أو ليست فاتورة نقطة بيع معتمدة.")


def pos_return(shift, user, invoice_name, lines, refund_method="cash", card=None):
    """مرتجع نقطة البيع: lines قائمة {"item": id, "qty": كمية موجبة}، ويُرد كامل المبلغ نقداً (cash) أو للبطاقة (card)."""
    if not shift or not shift.is_open:
        raise ValidationError("يجب فتح وردية قبل تنفيذ المرتجع.")
    if shift.user_id != user.id:
        raise ValidationError("هذه الوردية لا تخص المستخدم الحالي.")
    original = find_returnable_invoice(invoice_name or "")
    quantities = {}
    for line in lines:
        try:
            item_id = int(line.get("item"))
        except (TypeError, ValueError):
            raise ValidationError("صنف غير صالح.")
        quantities[item_id] = quantities.get(item_id, 0) + flt(line.get("qty"), 3)
    if not any(q > 0 for q in quantities.values()):
        raise ValidationError("حدد كمية واحدة على الأقل للإرجاع.")

    with transaction.atomic():
        ret = original.make_return(user, quantities)
        ret.is_pos, ret.pos_shift, ret.warehouse = True, shift, shift.profile.warehouse
        ret.calculate_totals()
        if refund_method not in ("cash", "card"):
            raise ValidationError("طريقة الرد غير صحيحة.")
        ret.paid_card = ret.grand_total if refund_method == "card" else flt(0)
        ret.paid_cash = ret.grand_total - ret.paid_card
        card_data = None
        if ret.paid_card:
            profile = shift.profile
            card_data = clean_card_info(card, profile.card_terminal_mode, ret.paid_card,
                                        profile.require_card_reference and profile.card_terminal_mode != "manual")
        ret.save()
        ret.submit()
        if card_data is not None:
            CardTransaction.objects.create(shift=shift, invoice=ret, kind="refund", amount=ret.paid_card,
                                           mode=shift.profile.card_terminal_mode, **card_data)
    return ret
