"""تسعير الأصناف: سجل تغيّر الأسعار، وتحديث سعر الشراء من آخر شراء، وتنبيه البيع بأقل من التكلفة."""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from core.utils import flt

# مصدر التغيير الحالي (يدوي / استيراد / شراء) ليُسجَّل مع كل تعديل سعر
_source = ContextVar("price_change_source", default=("manual", ""))


@contextmanager
def price_source(source, reference=""):
    token = _source.set((source, reference))
    try:
        yield
    finally:
        _source.reset(token)


def current_source():
    return _source.get()


def record_price_changes(item, old):
    """يُستدعى من حفظ الصنف: يسجل الفرق بين القيم القديمة والجديدة لسعري البيع والشراء."""
    from core.tenancy import get_current_user
    from stock.models import ItemPriceHistory

    source, reference = current_source()
    rows = []
    for field in ("selling_rate", "buying_rate"):
        before, after = flt(old.get(field) or 0), flt(getattr(item, field) or 0)
        if before != after:
            rows.append(ItemPriceHistory(company_id=item.company_id, item=item, field=field, old_rate=before,
                                         new_rate=after, source=source, reference=reference[:60],
                                         user=get_current_user()))
    if rows:
        ItemPriceHistory.objects.bulk_create(rows)


def net_unit_rate(doc, row):
    """سعر الوحدة بعملة الشركة قبل الضريبة من سطر مستند شراء."""
    rate = flt(Decimal(row.rate) / Decimal(getattr(row, "conversion_factor", 1) or 1))  # لكل وحدة أساسية
    if getattr(doc, "prices_include_tax", False) and doc.tax_rate:
        rate = flt(rate * 100 / (100 + flt(doc.tax_rate)))
    return flt(rate * flt(doc.exchange_rate or 1))


def last_purchase_rate(item, exclude_doc=None):
    """آخر سعر شراء معتمد للصنف من فواتير المشتريات وأذون الاستلام (بعملة الشركة قبل الضريبة)."""
    from buying.models import PurchaseInvoiceItem, PurchaseReceiptItem

    candidates = []
    for model in (PurchaseInvoiceItem, PurchaseReceiptItem):
        qs = model.objects.filter(item=item, parent__docstatus=1).select_related("parent")
        if model is PurchaseInvoiceItem:
            qs = qs.filter(parent__is_return=False)
        if exclude_doc is not None and isinstance(exclude_doc, model._meta.get_field("parent").related_model):
            qs = qs.exclude(parent=exclude_doc)
        row = qs.order_by("-parent__posting_date", "-parent__id", "-id").first()
        if row is not None:
            candidates.append(((row.parent.posting_date, row.parent.id), net_unit_rate(row.parent, row)))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]


def update_buying_rates(doc, cancelled=False):
    """بعد اعتماد شراء: سعر الشراء في بطاقة الصنف = آخر سعر شراء. وعند الإلغاء يعود للسعر السابق."""
    if not getattr(doc.company, "update_buying_rate_on_purchase", True) or getattr(doc, "is_return", False):
        return []
    alerts = []
    seen = set()
    with price_source("purchase", doc.name):
        for row in doc.items.select_related("item").order_by("id"):
            item = row.item
            if item.pk in seen or row.qty <= 0:
                continue
            seen.add(item.pk)
            rate = last_purchase_rate(item, exclude_doc=doc if cancelled else None)
            if rate is None or rate == item.buying_rate:
                continue
            item.buying_rate = rate
            item.save(update_fields=["buying_rate"])
            net_selling = selling_net(item, doc.company)
            if not cancelled and net_selling and rate >= net_selling:
                alerts.append((item, rate, net_selling))
    if alerts:
        from core.notify import notify_managers

        names = "، ".join(f"{i.name} (شراء {r} / بيع {s})" for i, r, s in alerts[:5])
        more = f" و{len(alerts) - 5} غيرها" if len(alerts) > 5 else ""
        try:
            notify_managers(doc.company, f"سعر الشراء أعلى من سعر البيع ({doc.name}): {names}{more}"[:250],
                            url="/reports/stock/pricing/?status=loss",
                            body="الأسعار قبل الضريبة. راجع «تحليل التسعير» لتعديل أسعار البيع.")
        except Exception:  # noqa: BLE001 — التنبيه لا يُفشل اعتماد الفاتورة
            pass
    return alerts


def selling_net(item, company):
    """سعر البيع قبل الضريبة (سعر البطاقة قد يكون شاملاً للضريبة حسب إعداد نقطة البيع)."""
    from stock.tools import prices_include_tax_default

    rate = flt(item.selling_rate)
    if prices_include_tax_default() and item.vat_category not in ("Z", "E", "O"):
        rate = flt(rate * 100 / (100 + flt(company.default_tax_rate)))
    return rate


def selling_from_net(net, item, company):
    """تحويل سعر قبل الضريبة إلى سعر البطاقة (شامل أو غير شامل حسب الإعداد)."""
    from stock.tools import prices_include_tax_default

    if prices_include_tax_default() and item.vat_category not in ("Z", "E", "O"):
        return flt(Decimal(net) * (100 + flt(company.default_tax_rate)) / 100)
    return flt(net)
