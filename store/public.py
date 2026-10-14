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
    """يحسب المحصّل لكل الفرص باستعلام واحد."""
    from store.models import DonationOrderLine, OrderStatus

    opportunities = list(opportunities)
    totals = dict(DonationOrderLine.objects.filter(opportunity__in=opportunities, order__status=OrderStatus.PAID)
                  .values("opportunity").annotate(t=Sum("amount")).values_list("opportunity", "t"))
    for opportunity in opportunities:
        opportunity._raised = flt(totals.get(opportunity.pk))
    return opportunities


def featured_opportunities(limit=6):
    """فرص التبرع المميزة للواجهة (وإن لم تُحدد مميزة فأحدث المنشورة):
    [{pk, title, url_name, url_args, image, raised, target, percent, kind_label}]."""
    qs = open_opportunities()
    featured = qs.filter(is_featured=True)
    items = attach_raised((featured if featured.exists() else qs)[:limit])
    return [{"pk": o.pk, "title": o.title, "url_name": "store_opportunity", "url_args": [o.pk], "image": o.image,
             "raised": o.raised, "target": o.target_amount, "percent": o.percent, "kind_label": o.get_kind_display()}
            for o in items]
