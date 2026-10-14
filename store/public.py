"""ما يعرضه موقع الجمعية من المتجر (يُستدعى من صفحات الموقع داخل سياق الجمعية)."""
from django.db.models import Q, Sum
from django.utils import timezone

from core.utils import flt


def open_opportunities():
    from store.models import DonationOpportunity

    today = timezone.localdate()
    return (DonationOpportunity.objects.filter(is_published=True)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today)))


def attach_raised(opportunities):
    """يحسب المحصّل وعدد المتبرعين لكل الفرص باستعلام واحد."""
    from django.db.models import Count

    from store.models import DonationOrderLine, OrderStatus

    opportunities = list(opportunities)
    rows = (DonationOrderLine.objects.filter(opportunity__in=opportunities, order__status=OrderStatus.PAID)
            .values("opportunity").annotate(t=Sum("amount"), n=Count("order", distinct=True)))
    totals = {row["opportunity"]: row for row in rows}
    for opportunity in opportunities:
        row = totals.get(opportunity.pk) or {}
        opportunity._raised = flt(row.get("t"))
        opportunity.donors = row.get("n", 0)
    return opportunities


def store_stats():
    """أرقام المتجر للواجهة: إجمالي المحصّل وعدد عمليات التبرع وعدد المتبرعين."""
    from django.db.models import Count

    from store.models import DonationOrder, OrderStatus

    paid = DonationOrder.objects.filter(status=OrderStatus.PAID)
    totals = paid.aggregate(t=Sum("total"), n=Count("id"), d=Count("donor_phone", distinct=True))
    return {"raised": flt(totals["t"]), "orders": totals["n"], "donors": totals["d"]}


def recent_donations(opportunity, limit=8):
    """آخر التبرعات المدفوعة لفرصة (المبلغ والوقت فقط، دون أسماء المتبرعين)."""
    from store.models import DonationOrderLine, OrderStatus

    return list(DonationOrderLine.objects.filter(opportunity=opportunity, order__status=OrderStatus.PAID)
                .select_related("order").only("amount", "quantity", "order__paid_at", "order__is_monthly")
                .order_by("-order__paid_at", "-id")[:limit])


def featured_opportunities(limit=6):
    """فرص التبرع المميزة للواجهة (وإن لم تُحدد مميزة فأحدث المنشورة):
    [{pk, title, url_name, url_args, image, raised, target, percent, kind_label}]."""
    qs = open_opportunities()
    featured = qs.filter(is_featured=True)
    items = attach_raised((featured if featured.exists() else qs)[:limit])
    return [{"pk": o.pk, "title": o.title, "url_name": "store_opportunity", "url_args": [o.pk], "image": o.image,
             "raised": o.raised, "target": o.target_amount, "percent": o.percent, "kind_label": o.get_kind_display()}
            for o in items]
