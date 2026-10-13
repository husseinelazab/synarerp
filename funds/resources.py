"""إدارة الموارد المالية: لوحة التبرعات والكفالات، وتوليد استحقاقات الشهر، وكشف حساب الكافل."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from beneficiaries.models import Beneficiary, BeneficiaryStatus
from core.utils import flt
from funds.models import (DonationReceipt, Donor, DueStatus, Sponsorship, SponsorshipDue, SponsorshipStatus,
                          generate_dues)

OPEN = [DueStatus.PENDING, DueStatus.PARTIAL]


@login_required
def resources_dashboard(request):
    today = timezone.localdate()
    month = today.replace(day=1)
    active = Sponsorship.objects.filter(status=SponsorshipStatus.ACTIVE)
    overdue = SponsorshipDue.objects.filter(status__in=OPEN, period__lt=month).select_related(
        "sponsorship__donor", "sponsorship__beneficiary")
    month_dues = SponsorshipDue.objects.filter(period=month)
    sponsored = active.values_list("beneficiary_id", flat=True)
    awaiting = (Beneficiary.objects.filter(status=BeneficiaryStatus.APPROVED, category__name="أيتام")
                .exclude(pk__in=sponsored).order_by("registered_on"))
    return render(request, "funds/resources_dashboard.html", {
        "title": "لوحة إدارة الموارد المالية", "month": month,
        "donations_month": DonationReceipt.objects.filter(docstatus=1, posting_date__gte=month)
        .aggregate(t=Sum("amount"), n=Count("id")),
        "by_type": DonationReceipt.objects.filter(docstatus=1, posting_date__year=today.year)
        .values("donation_type").annotate(t=Sum("amount")).order_by("-t"),
        "donation_types": dict(DonationReceipt._meta.get_field("donation_type").choices),
        "active_count": active.count(), "expected": flt(active.aggregate(t=Sum("monthly_amount"))["t"]),
        "month_generated": month_dues.exists(),
        "month_collected": flt(month_dues.aggregate(t=Sum("collected_amount"))["t"]),
        "overdue": overdue.order_by("period")[:100],
        "overdue_total": flt(sum((d.outstanding for d in overdue), flt(0))),
        "awaiting": awaiting[:50], "awaiting_count": awaiting.count(),
        "top_donors": Donor.objects.filter(receipts__docstatus=1, receipts__posting_date__year=today.year)
        .annotate(t=Sum("receipts__amount")).order_by("-t")[:10],
    })


@login_required
@require_POST
def generate_month_dues(request):
    created = generate_dues(timezone.localdate())
    messages.success(request, f"أُنشئ {created} استحقاقاً لهذا الشهر." if created else "استحقاقات هذا الشهر موجودة مسبقاً.")
    return redirect("resources_dashboard")


@login_required
def donor_statement(request, pk):
    donor = get_object_or_404(Donor, pk=pk)
    year = int(request.GET.get("year") or timezone.localdate().year)
    sponsorships = []
    for sponsorship in donor.sponsorships.select_related("beneficiary", "sponsorship_type"):
        dues = list(sponsorship.dues.filter(period__year=year).select_related("disbursement"))
        if not dues and sponsorship.status == SponsorshipStatus.ENDED:
            continue
        sponsorships.append({"sponsorship": sponsorship, "dues": dues,
                             "due_total": flt(sum((d.amount for d in dues if d.status != DueStatus.WAIVED), flt(0))),
                             "collected": flt(sum((d.collected_amount for d in dues), flt(0))),
                             "delivered": flt(sum((d.disbursement.amount for d in dues if d.disbursement_id
                                                   and d.disbursement.docstatus == 1), flt(0)))})
    receipts = donor.receipts.filter(docstatus=1, posting_date__year=year).order_by("posting_date")
    return render(request, "funds/donor_statement.html", {
        "title": f"كشف حساب الداعم — {donor.name}", "donor": donor, "year": year, "company": request.company,
        "sponsorships": sponsorships, "receipts": receipts,
        "receipts_total": flt(receipts.aggregate(t=Sum("amount"))["t"]),
    })
