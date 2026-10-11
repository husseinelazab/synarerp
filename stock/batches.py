"""التشغيلات وتواريخ الصلاحية.

- الوارد لصنف متتبَّع يدخل تشغيلة (من رقمها في المستند، أو تشغيلة الفاتورة الأصلية للمرتجع، أو تُنشأ تلقائياً).
- الصادر يُصرف من التشغيلة المحددة، وإلا فالأقرب انتهاءً أولاً (FEFO) مع منع المنتهية في البيع والإنتاج.
- التقييم يبقى بالمتوسط المرجح على مستوى الصنف والمستودع؛ التشغيلة تتابع الكميات والصلاحية فقط.
"""
import datetime

from django.core.exceptions import ValidationError
from django.db.models import Sum

from core.utils import flt


def locked_batch_bin(batch, warehouse):
    from stock.models import BatchBin

    BatchBin.all_objects.get_or_create(batch=batch, warehouse=warehouse, defaults={"company_id": batch.company_id})
    return BatchBin.all_objects.select_for_update().get(batch=batch, warehouse=warehouse)


def find_batch(item, batch_no):
    from stock.models import Batch

    return Batch.all_objects.filter(company_id=item.company_id, item=item, batch_no=str(batch_no).strip()).first()


def default_expiry(item, base_date):
    if item.shelf_life_days and base_date:
        return base_date + datetime.timedelta(days=item.shelf_life_days)
    return None


def get_or_create_batch(item, batch_no, doc=None, expiry_date=None, manufacturing_date=None):
    """تشغيلة برقمها؛ تُنشأ إن لم توجد. تاريخ الانتهاء المرسل يجب أن يطابق المسجل."""
    from stock.models import Batch

    batch_no = str(batch_no).strip()
    batch = find_batch(item, batch_no)
    if batch is None:
        posting = getattr(doc, "posting_date", None)
        batch = Batch.all_objects.create(
            company_id=item.company_id, item=item, batch_no=batch_no,
            manufacturing_date=manufacturing_date or posting,
            expiry_date=expiry_date or default_expiry(item, manufacturing_date or posting),
            reference=getattr(doc, "name", "") or "")
    elif expiry_date and batch.expiry_date and expiry_date != batch.expiry_date:
        raise ValidationError(f"التشغيلة {batch_no} للصنف «{item.name}» مسجلة بتاريخ انتهاء {batch.expiry_date}، "
                              f"والمستند يذكر {expiry_date}.")
    elif expiry_date and not batch.expiry_date:
        batch.expiry_date = expiry_date
        batch.save(update_fields=["expiry_date"])
    return batch


def auto_batch_no(item, doc):
    """رقم تشغيلة تلقائي: كود الصنف + تاريخ المستند، مع رقم تسلسلي عند التكرار."""
    from stock.models import Batch

    posting = getattr(doc, "posting_date", None) or datetime.date.today()
    base = f"{item.code}-{posting:%y%m%d}"
    existing = set(Batch.all_objects.filter(company_id=item.company_id, item=item, batch_no__startswith=base)
                   .values_list("batch_no", flat=True))
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def original_batches(doc, item):
    """تشغيلات الصنف في المستند الأصلي (للمرتجعات): [(تشغيلة، الكمية)] بالأكبر أولاً."""
    from stock.models import StockLedgerEntry

    original_id = getattr(doc, "return_against_id", None)
    if not original_id:
        return []
    rows = (StockLedgerEntry.all_objects.filter(voucher_type=doc.doctype_slug, voucher_id=original_id, item=item,
                                                 is_cancelled=False, batch__isnull=False)
            .values("batch_id").annotate(q=Sum("actual_qty")).order_by("-q"))
    from stock.models import Batch

    batches = Batch.all_objects.in_bulk([r["batch_id"] for r in rows])
    return [(batches[r["batch_id"]], abs(flt(r["q"], 3))) for r in rows]


def available_batches(item, warehouse, on_date, allow_expired=False):
    """التشغيلات المتاحة بترتيب الأقرب انتهاءً (بلا تاريخ انتهاء في الآخر)."""
    from stock.models import BatchBin

    bins = (BatchBin.all_objects.filter(batch__item=item, warehouse=warehouse, qty__gt=0)
            .select_related("batch"))
    result = []
    for b in bins:
        if not allow_expired and b.batch.is_expired(on_date):
            continue
        result.append((b.batch, flt(b.qty, 3)))
    far = datetime.date.max
    result.sort(key=lambda x: (x[0].expiry_date or far, x[0].id))
    return result


def plan_issue(doc, item, warehouse, qty, batch_no=""):
    """توزيع كمية صادرة على التشغيلات: [(تشغيلة، كمية)]."""
    on_date = getattr(doc, "posting_date", None)
    # allow_expired_batches: الصرف التلقائي يشمل المنتهية. allow_expired_explicit: يُسمح بها إذا حُدد رقمها (الإتلاف)
    allow_expired = getattr(doc, "allow_expired_batches", False)
    if batch_no:
        batch = find_batch(item, batch_no)
        if batch is None:
            raise ValidationError(f"التشغيلة {batch_no} غير موجودة للصنف «{item.name}».")
        if not (allow_expired or getattr(doc, "allow_expired_explicit", False)) and batch.is_expired(on_date):
            raise ValidationError(f"التشغيلة {batch.batch_no} من «{item.name}» منتهية الصلاحية "
                                  f"({batch.expiry_date}) ولا يمكن صرفها في {doc._meta.verbose_name}.")
        return [(batch, qty)]
    preferred = original_batches(doc, item)  # مرتجع المشتريات يعود من نفس تشغيلات الشراء
    plan, remaining = [], qty
    candidates = available_batches(item, warehouse, on_date, allow_expired)
    if preferred:
        order = {b.id: i for i, (b, _q) in enumerate(preferred)}
        candidates.sort(key=lambda c: order.get(c[0].id, len(order)))
    for batch, available in candidates:
        if remaining <= 0:
            break
        take = min(available, remaining)
        plan.append((batch, take))
        remaining = flt(remaining - take, 3)
    if remaining > 0:
        expired = sum((q for b, q in available_batches(item, warehouse, on_date, True) if b.is_expired(on_date)),
                      flt(0, 3))
        message = (f"الرصيد الصالح غير كافٍ من «{item.name}» في «{warehouse}»: المطلوب {qty} والمتاح "
                   f"{flt(qty - remaining, 3)}")
        if expired and not allow_expired:
            message += (f" (ويوجد {expired} منتهي الصلاحية لا يُصرف تلقائياً؛ أتلفه بحركة صرف مخزني "
                        f"مع رقم تشغيلته)")
        raise ValidationError(message + ".")
    return plan


def plan_receipt(doc, item, qty, batch_no="", expiry_date=None, manufacturing_date=None):
    """تشغيلة الوارد: المحددة، أو تشغيلة المستند الأصلي للمرتجع، أو تشغيلة جديدة تلقائية."""
    if batch_no:
        return [(get_or_create_batch(item, batch_no, doc, expiry_date, manufacturing_date), qty)]
    preferred = original_batches(doc, item)  # مرتجع المبيعات يعود لنفس التشغيلات التي بيعت
    if preferred:
        plan, remaining = [], qty
        for batch, sold in preferred:
            if remaining <= 0:
                break
            take = min(sold, remaining)
            plan.append((batch, take))
            remaining = flt(remaining - take, 3)
        if remaining > 0:
            plan[-1] = (plan[-1][0], flt(plan[-1][1] + remaining, 3))
        return plan
    return [(get_or_create_batch(item, auto_batch_no(item, doc), doc, expiry_date, manufacturing_date), qty)]


def sync_initial_batches(item):
    """عند تفعيل التتبع لصنف له رصيد: يوضع الرصيد غير الموزع في تشغيلة افتتاحية بلا تاريخ انتهاء."""
    from stock.models import Batch, BatchBin, Bin

    for b in Bin.all_objects.filter(item=item, actual_qty__gt=0).select_related("warehouse"):
        in_batches = flt(BatchBin.all_objects.filter(batch__item=item, warehouse=b.warehouse)
                         .aggregate(q=Sum("qty"))["q"] or 0, 3)
        missing = flt(b.actual_qty - in_batches, 3)
        if missing > 0:
            batch, _ = Batch.all_objects.get_or_create(
                company_id=item.company_id, item=item, batch_no=f"OPEN-{item.code}"[:40],
                defaults={"reference": "رصيد قبل تفعيل التتبع"})
            bb = locked_batch_bin(batch, b.warehouse)
            bb.qty = flt(bb.qty + missing, 3)
            bb.save()


def expiry_status(batch, warn_days=30, on_date=None):
    import django.utils.timezone as tz

    today = on_date or tz.localdate()
    if not batch.expiry_date:
        return "none"
    if batch.expiry_date < today:
        return "expired"
    if (batch.expiry_date - today).days <= warn_days:
        return "near"
    return "ok"
