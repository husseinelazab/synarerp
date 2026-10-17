"""منطق البيع في نقطة البيع: إنشاء فاتورة مبيعات مبسطة واعتمادها في عملية واحدة."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.utils import flt
from selling.models import Customer, SalesInvoice
from pos.models import CardTransaction, clean_card_info
from stock.models import Item


def pos_price_list(profile, customer):
    from pricing.engine import resolve_price_list

    return resolve_price_list("selling", customer, profile.price_list)


def build_lines(invoice, profile, lines):
    """أسطر الفاتورة من سلة الكاشير بسعر قائمة نقطة البيع (أو السعر المعدّل إن سُمح بتعديله)."""
    from pricing.engine import list_rate

    price_list = pos_price_list(profile, invoice.customer)
    for line in lines:
        try:
            item = Item.objects.get(pk=line.get("item"), company_id=profile.company_id, disabled=False)
        except (Item.DoesNotExist, ValueError, TypeError):
            raise ValidationError("صنف غير موجود أو موقوف.")
        qty = flt(line.get("qty"), 3)
        if qty <= 0:
            raise ValidationError(f"الكمية للصنف {item.name} يجب أن تكون أكبر من صفر.")
        uom_id = int(line["uom"]) if str(line.get("uom") or "").isdigit() else None
        # سعر الكرتون مثلاً، أو الأساسي × المعامل — من قائمة الأسعار إن وُجدت
        rate = list_rate(item, uom_id, "selling", price_list, invoice.posting_date)
        if profile.allow_rate_change and line.get("rate") not in (None, ""):
            rate = flt(line["rate"])
        invoice.items.create(item=item, qty=qty, rate=rate, uom_id=uom_id)


def checkout(shift, user, lines, customer_id=None, discount=0, paid_cash=0, paid_card=0, offline_id="",
             offline_at=None, card=None, coupon_code="", loyalty_points=0, apply_rules=True, credit_sale=False):
    """lines: قائمة من {"item": id, "qty": رقم, "rate": رقم اختياري}.

    offline_id: معرّف البيع من الجهاز عند البيع دون اتصال؛ إن سبقت مزامنته تُعاد نفس الفاتورة.
    apply_rules: العروض تُطبق في البيع المتصل؛ المبيعات دون اتصال تُفوتر بالأسعار التي رآها الكاشير.
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
    customer = Customer.objects.get(pk=customer_id, company_id=profile.company_id, disabled=False) if customer_id else profile.default_customer
    if credit_sale and (not customer_id or customer.pk == profile.default_customer_id):
        raise ValidationError("اختر عميلاً مسجلاً للشراء بالآجل؛ العميل النقدي الافتراضي غير مسموح.")
    if credit_sale and offline_id:
        raise ValidationError("البيع الآجل يتطلب اتصالاً للتحقق من ائتمان العميل.")
    discount = flt(discount)

    with transaction.atomic():
        # Serialize credit checks for the same customer across cashiers.
        customer = Customer.objects.select_for_update().get(pk=customer.pk, company_id=profile.company_id, disabled=False)
        invoice = SalesInvoice.objects.create(
            company=profile.company, customer=customer, is_pos=True, pos_credit_sale=credit_sale, pos_shift=shift, created_by=user,
            update_stock=True, warehouse=profile.warehouse, tax_rate=profile.tax_rate, cost_center=profile.cost_center,
            prices_include_tax=profile.prices_include_tax, discount_amount=discount,
            offline_id=offline_id or "", offline_at=offline_at, price_list=pos_price_list(profile, customer),
            coupon_code=(coupon_code or "").strip()[:40], loyalty_points=max(int(loyalty_points or 0), 0),
            ignore_pricing_rules=not (apply_rules and profile.apply_pricing_rules),
            **({"posting_date": timezone.localtime(offline_at).date()} if offline_at else {}),
        )
        build_lines(invoice, profile, lines)
        invoice.calculate_totals()

        if discount:
            if not profile.allow_discount:
                raise ValidationError("الخصم غير مسموح في نقطة البيع هذه.")
            if discount > flt(invoice.total * profile.max_discount_percent / 100):
                raise ValidationError(f"الخصم يتجاوز الحد المسموح ({profile.max_discount_percent}%).")

        invoice.paid_cash = flt(paid_cash)
        invoice.paid_card = flt(paid_card)
        invoice.change_amount = max(flt(invoice.paid_cash + invoice.paid_card - invoice.amount_due), flt(0))
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
        original = SalesInvoice.objects.select_for_update().get(pk=original.pk, company_id=shift.profile.company_id)
        ret = original.make_return(user, quantities)
        ret.is_pos, ret.pos_shift, ret.warehouse = True, shift, shift.profile.warehouse
        ret.calculate_totals()
        if refund_method not in ("cash", "card"):
            raise ValidationError("طريقة الرد غير صحيحة.")
        refund_total = flt(ret.grand_total + min(max(original.outstanding_amount, flt(0)), -ret.grand_total))
        ret.paid_card = refund_total if refund_method == "card" else flt(0)
        ret.paid_cash = refund_total - ret.paid_card
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


def quote(shift, lines, customer_id=None, discount=0, coupon_code="", loyalty_points=0):
    """تسعير السلة دون حفظ: الأسعار بعد العروض والهدايا والإجماليات والنقاط، لعرضها للكاشير قبل الدفع."""
    from pricing.engine import points_balance, program_for

    if not shift or not shift.is_open:
        raise ValidationError("يجب فتح وردية قبل البيع.")
    profile = shift.profile
    customer = Customer.objects.get(pk=customer_id, company_id=profile.company_id, disabled=False) if customer_id else profile.default_customer
    with transaction.atomic():
        # فاتورة مؤقتة تُحسب ثم تُلغى المعاملة كاملة فلا يُحفظ شيء ولا يُستهلك رقم
        invoice = SalesInvoice.objects.create(
            company=profile.company, customer=customer, is_pos=True, pos_shift=shift, update_stock=False,
            tax_rate=profile.tax_rate, cost_center=profile.cost_center, prices_include_tax=profile.prices_include_tax,
            discount_amount=flt(discount), price_list=pos_price_list(profile, customer),
            coupon_code=(coupon_code or "").strip()[:40], loyalty_points=max(int(loyalty_points or 0), 0),
            ignore_pricing_rules=not profile.apply_pricing_rules)
        build_lines(invoice, profile, lines)
        invoice.calculate_totals()
        program = program_for(customer)
        result = {
            "lines": [{"item": r.item_id, "name": r.item.name, "qty": float(r.qty), "uom": r.uom_id,
                       "rate": float(r.rate), "price_list_rate": float(r.price_list_rate),
                       "discount_percentage": float(r.discount_percentage), "free": r.is_free_item,
                       "rule": r.pricing_rule.name if r.pricing_rule_id else ""}
                      for r in invoice.items.select_related("item", "pricing_rule").order_by("id")],
            "total": float(invoice.total), "discount": float(invoice.discount_amount), "net": float(invoice.net_total),
            "tax": float(invoice.tax_amount), "grand": float(invoice.grand_total),
            "loyalty_amount": float(invoice.loyalty_amount), "due": float(invoice.amount_due),
            "coupon_ok": not invoice.coupon_code or any(
                r.pricing_rule_id and r.pricing_rule.coupon_code == invoice.coupon_code.upper()
                for r in invoice.items.select_related("pricing_rule")),
            "points": points_balance(customer) if program else None,
            "point_value": float(program.point_value) if program else 0,
        }
        from stock.models import Bin
        required = {}
        for row in invoice.items.select_related("item"):
            if row.item.is_stock_item:
                entry = required.setdefault(row.item_id, [row.item, Decimal(0)])
                entry[1] += row.stock_qty
        balances = {b.item_id:b for b in Bin.objects.filter(warehouse=profile.warehouse, item_id__in=required)}
        result["stock_warnings"] = []
        for item_id, (item, qty) in required.items():
            balance = balances.get(item_id)
            actual = balance.actual_qty if balance else Decimal(0)
            if qty > actual:
                cost = balance.valuation_rate if balance and balance.valuation_rate > 0 else item.buying_rate
                allowed = profile.allow_negative_stock and not item.has_batch_no and not item.has_serial_no and cost > 0
                result["stock_warnings"].append({"name":item.name,"available":float(actual),"after":float(actual-qty),"allowed":allowed})
        transaction.set_rollback(True)
    return result
