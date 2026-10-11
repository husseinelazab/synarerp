"""محرك التسعير: سعر القائمة ← أفضل قاعدة تسعير ← الهدايا، لأسطر مستندات البيع والشراء.

يُطبق على المسودات عند كل حفظ (والسطر المرتبط بأمر أو عرض سعر يحتفظ بسعره المنقول)،
ونقاط البيع تطلب نفس التسعير من الخادم قبل الدفع ليطابق المعروض للكاشير ما يُفوتر.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q

from core.utils import flt


def doc_kind(doc):
    from buying.models import Supplier  # noqa: F401  (التأكد من تحميل التطبيق)

    return "selling" if getattr(doc, "party_field", "customer") == "customer" else "buying"


def resolve_price_list(kind, party=None, explicit=None):
    """قائمة المستند إن حُددت، وإلا قائمة الطرف، ثم قائمة فئته، ثم القائمة الافتراضية للنوع."""
    from pricing.models import PriceList

    candidates = [explicit, getattr(party, "price_list", None)]
    group = getattr(party, "customer_group", None)
    if group is not None:
        candidates.append(group.price_list)
    for price_list in candidates:
        if price_list is not None and not price_list.disabled and price_list.kind == kind:
            return price_list
    return PriceList.objects.filter(kind=kind, is_default=True, disabled=False).first()


def _list_currency_ok(price_list, currency_id, company):
    list_currency = price_list.currency_id or company.currency_id
    return currency_id is None or list_currency == currency_id


def list_rate(item, uom_id=None, kind="selling", price_list=None, date=None, currency_id=None, company=None):
    """سعر الصنف بوحدة السطر: من قائمة الأسعار إن وُجد فيها، وإلا من بطاقة الصنف."""
    from pricing.models import ItemPrice

    company = company or item.company
    if price_list is not None and _list_currency_ok(price_list, currency_id, company):
        prices = ItemPrice.objects.filter(price_list=price_list, item=item)
        if date is not None:
            prices = prices.filter(Q(valid_from__isnull=True) | Q(valid_from__lte=date),
                                   Q(valid_upto__isnull=True) | Q(valid_upto__gte=date))
        prices = list(prices.order_by("-valid_from", "-id"))
        if uom_id and uom_id != item.uom_id:
            exact = next((p for p in prices if p.uom_id == uom_id), None)
            if exact is not None:
                return flt(exact.rate)
        base = next((p for p in prices if p.uom_id in (None, item.uom_id)), None)
        if base is not None:
            return flt(Decimal(base.rate) * item.uom_factor(uom_id))
    return item.price_for(uom_id, kind)


def _group_ids(group):
    ids = []
    while group is not None and group.pk not in ids:
        ids.append(group.pk)
        group = group.parent
    return ids


def candidate_rules(kind, item, party=None, date=None, coupon="", channel="backoffice"):
    """القواعد السارية على الصنف والطرف والتاريخ والقناة والكوبون (قبل شرط الكمية)."""
    from pricing.models import ApplyOn, Channel, PricingRule

    rules = PricingRule.objects.filter(kind=kind, disabled=False)
    if date is not None:
        rules = rules.filter(Q(valid_from__isnull=True) | Q(valid_from__lte=date),
                             Q(valid_upto__isnull=True) | Q(valid_upto__gte=date))
    rules = rules.filter(channel__in=[Channel.ALL, channel])
    rules = rules.filter(Q(apply_on=ApplyOn.ALL) | Q(apply_on=ApplyOn.ITEM, item=item) |
                         Q(apply_on=ApplyOn.ITEM_GROUP, item_group_id__in=_group_ids(item.item_group)))
    if kind == "selling":
        group_id = getattr(party, "customer_group_id", None)
        rules = rules.filter(Q(customer__isnull=True) | Q(customer=party) if party is not None else Q(customer__isnull=True))
        rules = rules.filter(Q(customer_group__isnull=True) | Q(customer_group_id=group_id) if group_id
                             else Q(customer_group__isnull=True))
        coupon = (coupon or "").strip().upper()
        rules = rules.filter(Q(coupon_code="") | Q(coupon_code=coupon)) if coupon else rules.filter(coupon_code="")
    else:
        rules = rules.filter(Q(supplier__isnull=True) | Q(supplier=party) if party is not None else Q(supplier__isnull=True))
    return [r for r in rules.select_related("free_item") if not r.coupon_exhausted]


def _specificity(rule):
    item_level = {"item": 2, "item_group": 1, "all": 0}[rule.apply_on]
    party_level = 2 if (rule.customer_id or rule.supplier_id) else 1 if rule.customer_group_id else 0
    return (rule.priority, item_level, party_level, bool(rule.coupon_code))


def _qty_ok(rule, qty):
    return qty >= flt(rule.min_qty, 3) and (not rule.max_qty or qty <= flt(rule.max_qty, 3))


def discounted_rate(rule, price_list_rate, factor=1):
    """سعر الوحدة بعد القاعدة (factor: معامل وحدة السطر لتحويل المبالغ المحددة للوحدة الأساسية)."""
    from pricing.models import DiscountType

    factor = Decimal(factor or 1)
    if rule.discount_type == DiscountType.PERCENTAGE:
        return flt(price_list_rate * (100 - flt(rule.discount_percentage)) / 100)
    if rule.discount_type == DiscountType.AMOUNT:
        return max(flt(price_list_rate - flt(Decimal(rule.discount_amount) * factor)), flt(0))
    if rule.discount_type == DiscountType.RATE:
        return flt(Decimal(rule.rate) * factor)
    return flt(price_list_rate)


def best_rules(rules, qty):
    """(أفضل قاعدة سعر، أفضل قاعدة هدية) للكمية."""
    from pricing.models import DiscountType

    eligible = sorted((r for r in rules if _qty_ok(r, qty)), key=_specificity, reverse=True)
    price = next((r for r in eligible if r.discount_type != DiscountType.NONE), None)
    gift = next((r for r in eligible if r.free_item_id), None)
    return price, gift


def free_quantity(rule, qty):
    if not rule.recurse or not rule.min_qty:
        return flt(rule.free_qty, 3)
    return flt(int(qty // flt(rule.min_qty, 3)) * flt(rule.free_qty, 3), 3)


def price_line(item, qty, uom_id=None, kind="selling", party=None, price_list=None, date=None, coupon="",
               channel="backoffice", currency_id=None, ignore_rules=False):
    """تسعير سطر واحد: {price_list_rate, rate, discount_percentage, rule, gift_rule, free_qty}."""
    factor = item.uom_factor(uom_id)
    base = list_rate(item, uom_id, kind, price_list, date, currency_id)
    result = {"price_list_rate": base, "rate": base, "discount_percentage": flt(0), "rule": None,
              "gift_rule": None, "free_qty": flt(0, 3)}
    if ignore_rules:
        return result
    stock_qty = flt(Decimal(qty or 0) * factor, 3)
    rule, gift = best_rules(candidate_rules(kind, item, party, date, coupon, channel), stock_qty)
    if rule is not None:
        result["rule"] = rule
        result["rate"] = discounted_rate(rule, base, factor)
        if base:
            result["discount_percentage"] = flt((base - result["rate"]) * 100 / base)
    if gift is not None:
        free = free_quantity(gift, stock_qty)
        if free > 0:
            result["gift_rule"], result["free_qty"] = gift, free
    return result


def apply_pricing(doc, channel=None):
    """يطبق التسعير على أسطر مسودة المستند ويضيف أسطر الهدايا. يُستدعى قبل حساب الإجماليات."""
    if not doc.pk or not doc.is_draft or getattr(doc, "is_return", False) or getattr(doc, "is_debit_note", False):
        return
    kind = doc_kind(doc)
    party = doc.party if getattr(doc, f"{doc.party_field}_id", None) else None
    price_list = resolve_price_list(kind, party, doc.price_list)
    if channel is None:
        channel = "pos" if getattr(doc, "is_pos", False) else "backoffice"
    coupon = getattr(doc, "coupon_code", "") if kind == "selling" else ""
    rows = list(doc.items.select_related("item", "item__item_group"))
    for row in [r for r in rows if r.is_free_item]:
        row.delete()
    gifts = {}
    for row in [r for r in rows if not r.is_free_item]:
        if _is_linked(row):
            continue  # سطر منقول من أمر/عرض/إذن: يحتفظ بسعره المتفق عليه
        priced = price_line(row.item, row.qty, row.uom_id, kind, party, price_list, doc.posting_date, coupon,
                            channel, doc.currency_id, ignore_rules=doc.ignore_pricing_rules)
        update = ["price_list_rate", "discount_percentage", "pricing_rule"]
        had_rule = row.pricing_rule_id is not None
        row.price_list_rate = priced["price_list_rate"]
        if priced["rule"] is not None:
            row.rate = priced["rate"]
            row.discount_percentage = priced["discount_percentage"]
            row.pricing_rule = priced["rule"]
            update.append("rate")
        else:
            row.pricing_rule = None
            row.discount_percentage = flt(0)
            if had_rule or not flt(row.rate):
                # زالت القاعدة (تغيرت الكمية مثلاً) أو السعر فارغ: يعود سعر القائمة
                row.rate = priced["price_list_rate"]
                update.append("rate")
        row.save(update_fields=update)
        if priced["gift_rule"] is not None:
            key = (priced["gift_rule"].pk, priced["gift_rule"].free_item_id)
            entry = gifts.setdefault(key, [priced["gift_rule"], flt(0, 3)])
            entry[1] += priced["free_qty"]
    for rule, qty in gifts.values():
        doc.items.create(item=rule.free_item, qty=qty, rate=0, is_free_item=True, pricing_rule=rule,
                         price_list_rate=list_rate(rule.free_item, None, kind, price_list, doc.posting_date,
                                                   doc.currency_id),
                         discount_percentage=100)


def _is_linked(row):
    return any(getattr(row, f"{name}_id", None) for name in ("order_item", "delivery_note_item", "quotation_item",
                                                              "receipt_item"))


def used_coupon_rules(doc):
    from pricing.models import PricingRule

    ids = {row.pricing_rule_id for row in doc.items.all() if row.pricing_rule_id}
    return list(PricingRule.objects.select_for_update().filter(pk__in=ids).exclude(coupon_code=""))


def consume_coupons(doc, sign):
    """عند اعتماد المستند (+1) أو إلغائه (-1) يُحدث عداد استخدام الكوبونات المطبقة."""
    for rule in used_coupon_rules(doc):
        if sign > 0 and rule.max_uses and rule.used_count >= rule.max_uses:
            raise ValidationError(f"استُنفد كوبون «{rule.coupon_code}» ({rule.max_uses} استخدام).")
        rule.used_count = max(rule.used_count + sign, 0)
        rule.save(update_fields=["used_count"])


# ---------- نقاط الولاء ----------

def program_for(customer):
    from pricing.models import LoyaltyProgram

    if customer is None:
        return None
    program = customer.loyalty_program
    if program is not None:
        return None if program.disabled else program
    program = LoyaltyProgram.objects.filter(auto_enroll=True, disabled=False).first()
    if program is None:
        return None
    from pos.models import POSProfile

    if POSProfile.objects.filter(default_customer=customer).exists():
        return None  # العميل النقدي الافتراضي لا يجمع نقاطاً
    return program


def points_balance(customer, date=None):
    from django.db.models import Sum
    from django.utils import timezone

    from pricing.models import LoyaltyPointEntry

    date = date or timezone.localdate()
    entries = LoyaltyPointEntry.objects.filter(customer=customer)
    earned = entries.filter(points__gt=0).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=date))
    spent = entries.filter(points__lt=0)
    return int((earned.aggregate(t=Sum("points"))["t"] or 0) + (spent.aggregate(t=Sum("points"))["t"] or 0))
