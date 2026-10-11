"""أعمار الديون والرقابة الائتمانية على العملاء."""
from collections import OrderedDict

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from core.utils import flt

BUCKETS = [(0, "غير مستحقة بعد"), (30, "1 - 30 يوماً"), (60, "31 - 60 يوماً"), (90, "61 - 90 يوماً"),
           (None, "أكثر من 90 يوماً")]


def party_balance(party_field, party):
    """رصيد الطرف من دفتر الأستاذ (موجب = مستحق لنا على العميل / لنا عند المورد مقلوباً)."""
    from accounts.models import AccountType, GLEntry

    account_type = AccountType.RECEIVABLE if party_field == "customer" else AccountType.PAYABLE
    agg = GLEntry.objects.filter(**{party_field: party}, account__account_type=account_type,
                                 is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
    balance = flt((agg["d"] or 0) - (agg["c"] or 0))
    return balance if party_field == "customer" else -balance


def _bucket(days):
    if days <= 0:
        return 0
    for i, (limit, _label) in enumerate(BUCKETS[1:], start=1):
        if limit is None or days <= limit:
            return i
    return len(BUCKETS) - 1


def aging(party_field="customer", basis="due", today=None, party=None):
    """أعمار الأرصدة لكل طرف حسب الفواتير غير المسددة، مع الرصيد غير المخصص (دفعات مقدمة/مرتجعات غير مربوطة)."""
    from buying.models import PurchaseInvoice
    from selling.models import SalesInvoice

    model = SalesInvoice if party_field == "customer" else PurchaseInvoice
    today = today or timezone.localdate()
    qs = model.objects.filter(docstatus=1).exclude(outstanding_amount=0).select_related(party_field)
    if party is not None:
        qs = qs.filter(**{party_field: party})
    rows = OrderedDict()
    for inv in qs.order_by(f"{party_field}__name", "posting_date"):
        p = getattr(inv, party_field)
        row = rows.setdefault(p.pk, {"party": p, "buckets": [flt(0)] * len(BUCKETS), "invoices": 0,
                                     "outstanding": flt(0), "oldest": None})
        ref = inv.due_date if basis == "due" and inv.due_date else inv.posting_date
        days = (today - ref).days
        amount = flt(inv.outstanding_amount)
        if amount < 0:  # مرتجع غير مسوّى يُخصم من الحالي
            row["buckets"][0] += amount
        else:
            row["buckets"][_bucket(days)] += amount
            if days > 0 and (row["oldest"] is None or days > row["oldest"]):
                row["oldest"] = days
        row["invoices"] += 1
        row["outstanding"] += amount
    result = []
    for row in rows.values():
        balance = party_balance(party_field, row["party"])
        row["balance"] = balance
        row["unallocated"] = flt(balance - row["outstanding"])
        row["overdue"] = sum(row["buckets"][1:], flt(0))
        limit = flt(getattr(row["party"], "credit_limit", 0) or 0)
        row["credit_limit"] = limit
        row["over_limit"] = bool(limit and balance > limit)
        result.append(row)
    return result


def overdue_invoices(customer, older_than_days=0, today=None):
    from selling.models import SalesInvoice

    today = today or timezone.localdate()
    cutoff = today - timezone.timedelta(days=older_than_days)
    return SalesInvoice.objects.filter(customer=customer, docstatus=1, is_return=False, outstanding_amount__gt=0,
                                       due_date__lt=cutoff)


def check_credit(doc, exposure):
    """يمنع البيع الآجل إن كان العميل موقوفاً أو تجاوز حد الائتمان أو لديه فواتير متأخرة أكثر من المسموح.

    exposure: المبلغ الآجل الجديد في هذا المستند (بعملة الشركة).
    """
    customer = doc.customer
    exposure = flt(exposure)
    if exposure <= 0:
        return
    if customer.credit_hold:
        raise ValidationError(f"البيع الآجل للعميل «{customer.name}» موقوف من بطاقته. "
                              "يمكن البيع نقداً، أو ألغِ الإيقاف من بطاقة العميل.")
    days = doc.company.block_overdue_days
    if days:
        late = overdue_invoices(customer, days).order_by("due_date").first()
        if late is not None:
            raise ValidationError(f"للعميل «{customer.name}» فواتير متأخرة أكثر من {days} يوماً (أقدمها {late.name} "
                                  f"بمتبقٍ {late.outstanding_amount}). حصّلها أولاً أو بِع نقداً.")
    limit = flt(customer.credit_limit or 0)
    if limit:
        balance = party_balance("customer", customer)
        if balance + exposure > limit:
            raise ValidationError(
                f"تجاوز حد الائتمان للعميل «{customer.name}»: الحد {limit:,.2f}، الرصيد الحالي {balance:,.2f}، "
                f"وهذا المستند يضيف {exposure:,.2f} (المتاح {max(limit - balance, 0):,.2f}). "
                "حصّل جزءاً من الرصيد أو ارفع الحد من بطاقة العميل.")


def credit_status(customer):
    """ملخص ائتماني لبطاقة العميل."""
    balance = party_balance("customer", customer)
    limit = flt(customer.credit_limit or 0)
    rows = aging("customer", party=customer)
    buckets = rows[0]["buckets"] if rows else [flt(0)] * len(BUCKETS)
    return {"balance": balance, "limit": limit, "available": flt(limit - balance) if limit else None,
            "used_percent": flt(balance * 100 / limit) if limit else None,
            "buckets": list(zip([b[1] for b in BUCKETS], buckets)), "overdue": sum(buckets[1:], flt(0)),
            "hold": customer.credit_hold}
