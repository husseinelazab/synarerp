"""تقييم الشرائح إلى قائمة مستلمين بلا تكرار (حسب الجوال الموحّد أو البريد)."""
import datetime

from django.db.models import Max, Sum
from django.utils import timezone

from campaigns.models import SegmentRule
from campaigns.service import norm_email, norm_phone
from funds.models import add_months


class Collector:
    """يجمع المستلمين ويدمج المكرر: نفس الجوال أو نفس البريد = شخص واحد."""

    def __init__(self, company):
        self.company = company
        self.rows = []
        self.index = {}

    def add(self, name, phone="", email="", source=""):
        phone = norm_phone(phone, self.company)
        email = norm_email(email)
        if not phone and not email:
            return
        keys = [k for k in (phone and f"p:{phone}", email and f"e:{email}") if k]
        existing = next((self.index[k] for k in keys if k in self.index), None)
        if existing is None:
            existing = {"name": (name or "").strip(), "phone": phone, "email": email, "source": source}
            self.rows.append(existing)
        else:
            existing["phone"] = existing["phone"] or phone
            existing["email"] = existing["email"] or email
            existing["name"] = existing["name"] or (name or "").strip()
        for k in keys:
            self.index[k] = existing


def _donors(company):
    from funds.models import Donor

    return Donor.all_objects.filter(company=company, is_active=True)


def _add_donors(col, donors, source):
    for d in donors.order_by("name"):
        col.add(d.name, d.phone, d.email, source)


def evaluate(segment):
    from funds.models import DonationReceipt, DueStatus, SponsorshipDue, SponsorshipStatus

    company = segment.company
    col = Collector(company)
    today = timezone.localdate()
    rule = segment.rule
    receipts = DonationReceipt.all_objects.filter(company=company, docstatus=1)

    if rule == SegmentRule.ALL_DONORS:
        _add_donors(col, _donors(company), "الداعمون")
    elif rule == SegmentRule.LAPSED:
        cutoff = _months_ago(today, segment.months or 6)
        last = (receipts.filter(donor__isnull=False).values("donor").annotate(last=Max("posting_date"))
                .filter(last__lt=cutoff).values_list("donor", flat=True))
        _add_donors(col, _donors(company).filter(pk__in=list(last)), "منقطعون")
    elif rule == SegmentRule.MAJOR:
        since = _months_ago(today, 12)
        major = (receipts.filter(donor__isnull=False, posting_date__gte=since).values("donor")
                 .annotate(total=Sum("amount")).filter(total__gte=segment.min_amount).values_list("donor", flat=True))
        _add_donors(col, _donors(company).filter(pk__in=list(major)), "كبار الداعمين")
    elif rule == SegmentRule.ACTIVE_SPONSORS:
        _add_donors(col, _donors(company).filter(sponsorships__status=SponsorshipStatus.ACTIVE).distinct(),
                    "الكافلون")
    elif rule == SegmentRule.OVERDUE_SPONSORS:
        month = today.replace(day=1)
        ids = (SponsorshipDue.all_objects.filter(company=company, period__lt=month,
                                                 status__in=[DueStatus.PENDING, DueStatus.PARTIAL])
               .values_list("sponsorship__donor", flat=True))
        _add_donors(col, _donors(company).filter(pk__in=list(ids)), "كافلون متأخرون")
    elif rule == SegmentRule.RECURRING:
        from store.models import RecurringDonation

        for r in RecurringDonation.all_objects.filter(company=company, active=True).order_by("id"):
            col.add(r.donor_name, r.donor_phone, "", "تبرع شهري")
    elif rule == SegmentRule.STORE_DONORS:
        from store.models import DonationOrder, OrderStatus

        for o in DonationOrder.all_objects.filter(company=company, status=OrderStatus.PAID).order_by("-id"):
            col.add(o.donor_name, o.donor_phone, o.donor_email, "المتجر")
    elif rule == SegmentRule.NEWSLETTER:
        from website.models import NewsletterSubscriber

        for s in NewsletterSubscriber.all_objects.filter(company=company, is_active=True).order_by("id"):
            col.add("", "", s.email, "النشرة البريدية")
    elif rule == SegmentRule.FUND_DONORS:
        if segment.fund_id:
            for r in receipts.filter(fund_id=segment.fund_id).select_related("donor").order_by("-id"):
                donor = r.donor
                col.add(r.donor_name or (donor.name if donor else ""), r.donor_phone or (donor.phone if donor else ""),
                        donor.email if donor else "", "سندات التبرع")
        if segment.opportunity_kind:
            from store.models import DonationOrder, OrderStatus

            orders = (DonationOrder.all_objects.filter(company=company, status=OrderStatus.PAID,
                                                       lines__opportunity__kind=segment.opportunity_kind)
                      .distinct().order_by("-id"))
            for o in orders:
                col.add(o.donor_name, o.donor_phone, o.donor_email, "المتجر")
    return col.rows


def _months_ago(today, months):
    first = add_months(today.replace(day=1), -months)
    try:
        return first.replace(day=today.day)
    except ValueError:  # نهاية شهر أقصر
        return add_months(first, 1) - datetime.timedelta(days=1)
