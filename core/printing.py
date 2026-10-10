"""تجهيز بيانات المستندات للطباعة: العناوين، وأسطر الأصناف مع ضريبة كل سطر، والمجاميع، والتفقيط."""
from decimal import Decimal

from core.tafqeet import amount_in_words
from core.utils import allocate, flt

# الاسم العربي والإنجليزي لكل نوع مستند
TITLES = {
    "فاتورة ضريبية": "Tax Invoice",
    "فاتورة ضريبية مبسطة": "Simplified Tax Invoice",
    "إشعار دائن (مرتجع مبيعات)": "Credit Note",
    "إشعار دائن مبسط": "Simplified Credit Note",
    "إشعار مدين ضريبي": "Debit Note",
    "إشعار مدين مبسط": "Simplified Debit Note",
    "فاتورة مشتريات": "Purchase Invoice",
    "إشعار مدين (مرتجع مشتريات)": "Purchase Return",
    "عرض سعر": "Quotation",
    "أمر بيع": "Sales Order",
    "أمر شراء": "Purchase Order",
    "إذن تسليم": "Delivery Note",
    "إذن استلام": "Goods Received Note",
}
TRADE_SLUGS = {"sales-invoice", "purchase-invoice", "quotation", "sales-order", "purchase-order"}
FULFILLMENT_SLUGS = {"delivery-note", "purchase-receipt"}


def party_info(party):
    """بيانات العميل أو المورد للطباعة."""
    if party is None:
        return None
    parts = [getattr(party, f, "") for f in ("building_number", "street", "district", "city", "postal_code")]
    address = "، ".join(p for p in parts if p) or getattr(party, "address", "") or getattr(party, "city", "")
    if getattr(party, "address", "") and address != party.address and not any(parts[:3]):
        address = party.address
    return {"name": party.name, "tax_id": getattr(party, "tax_id", ""), "phone": getattr(party, "phone", ""),
            "email": getattr(party, "email", ""), "address": address}


def trade_lines(doc):
    """أسطر الفاتورة أو الأمر: سعر الوحدة قبل الضريبة، والخصم الموزع، والمبلغ الخاضع، والضريبة، والإجمالي.

    القيم موزعة بحيث يساوي مجموع الأسطر مجاميع المستند تماماً.
    """
    rows = list(doc.items.select_related("item", "item__uom").order_by("id"))
    if not rows:
        return [], False
    rate = flt(doc.tax_rate)
    weights = {r.pk: abs(r.amount) for r in rows}
    if not any(weights.values()):
        weights = {r.pk: 1 for r in rows}
    discount = abs(flt(doc.discount_amount))
    if doc.prices_include_tax:
        discount = flt(discount * 100 / (100 + rate)) if discount else flt(0)
        gross = allocate(abs(doc.net_total) + discount, weights)
    else:
        gross = {r.pk: abs(r.amount) for r in rows}
    discounts = allocate(discount, weights) if discount else {r.pk: flt(0) for r in rows}
    taxes = allocate(abs(doc.tax_amount), weights)
    lines = []
    for r in rows:
        qty = abs(r.qty)
        taxable = gross[r.pk] - discounts[r.pk]
        lines.append({
            "code": r.item.code, "name": r.item.name, "uom": str(r.item.uom) if r.item.uom_id else "",
            "qty": qty, "unit_price": (gross[r.pk] / qty).quantize(Decimal("0.01")) if qty else gross[r.pk],
            "gross": gross[r.pk], "discount": discounts[r.pk], "taxable": taxable, "tax_rate": rate,
            "tax": taxes[r.pk], "total": taxable + taxes[r.pk],
        })
    return lines, bool(discount)


def fulfillment_lines(doc):
    rows = doc.items.select_related("item", "item__uom").order_by("id")
    return [{"code": r.item.code, "name": r.item.name, "uom": str(r.item.uom) if r.item.uom_id else "",
             "qty": abs(r.qty)} for r in rows]


def print_context(slug, doc, company, settings):
    title = getattr(doc, "document_title", "") or doc._meta.verbose_name
    party = getattr(doc, "customer", None) if hasattr(doc, "customer_id") else getattr(doc, "supplier", None)
    currency = getattr(doc, "currency", None) or company.currency
    ctx = {
        "title": title, "title_en": TITLES.get(title, ""), "party": party_info(party),
        "party_label": ("العميل", "Customer") if hasattr(doc, "customer_id") else ("المورد", "Supplier"),
        "is_sales": slug in ("sales-invoice", "quotation", "sales-order", "delivery-note"),
        "currency": currency, "kind": "generic",
    }
    if slug in TRADE_SLUGS:
        lines, has_discount = trade_lines(doc)
        ctx.update(kind="trade", lines=lines, has_discount=has_discount)
        ctx["totals"] = {
            "gross": sum((ln["gross"] for ln in lines), flt(0)), "discount": sum((ln["discount"] for ln in lines), flt(0)),
            "taxable": abs(doc.net_total), "tax": abs(doc.tax_amount), "grand": abs(doc.grand_total),
        }
        if settings.show_amount_in_words:
            ctx["amount_words"] = amount_in_words(abs(doc.grand_total), currency.code, currency.name)
        ctx["terms"] = getattr(doc, "terms", "") or (settings.sales_terms if slug == "sales-invoice" else "")
        ctx["show_bank"] = (settings.show_bank_details and settings.iban and ctx["is_sales"]
                            and not getattr(doc, "is_return", False))
    elif slug in FULFILLMENT_SLUGS:
        ctx.update(kind="fulfillment", lines=fulfillment_lines(doc))
    return ctx
