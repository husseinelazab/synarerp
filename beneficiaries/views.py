from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import render
from django.utils import timezone

from beneficiaries.models import (Beneficiary, BeneficiaryStatus, CaseAssessment, EntityRequest, RequestStatus)


@login_required
def beneficiaries_dashboard(request):
    today = timezone.localdate()
    active = Beneficiary.objects.exclude(status=BeneficiaryStatus.CLOSED)
    return render(request, "beneficiaries/dashboard.html", {
        "title": "لوحة إدارة التقييم والمتابعة",
        "total": active.count(),
        "by_status": active.values("status").annotate(n=Count("id")).order_by("status"),
        "statuses": dict(BeneficiaryStatus.choices),
        "by_category": active.filter(status=BeneficiaryStatus.APPROVED).values("category__name")
        .annotate(n=Count("id")).order_by("-n"),
        "pending_study": active.filter(status__in=[BeneficiaryStatus.NEW, BeneficiaryStatus.UNDER_STUDY])
        .select_related("researcher").order_by("registered_on")[:50],
        "review_due": active.filter(status=BeneficiaryStatus.APPROVED, next_review_date__lte=today)
        .order_by("next_review_date")[:50],
        "draft_assessments": CaseAssessment.objects.filter(docstatus=0).select_related("beneficiary", "researcher")[:50],
    })


@login_required
def entities_dashboard(request):
    open_requests = EntityRequest.objects.filter(
        status__in=[RequestStatus.NEW, RequestStatus.UNDER_REVIEW, RequestStatus.APPROVED]).select_related("entity")
    year = timezone.localdate().year
    approved = EntityRequest.objects.filter(status__in=[RequestStatus.APPROVED, RequestStatus.COMPLETED],
                                            request_date__year=year)
    return render(request, "beneficiaries/entities_dashboard.html", {
        "title": "لوحة إدارة الجهات المستفيدة",
        "open_requests": open_requests.order_by("request_date"),
        "by_status": EntityRequest.objects.values("status").annotate(n=Count("id")).order_by("status"),
        "statuses": dict(RequestStatus.choices),
        "approved_total": approved.aggregate(t=Sum("approved_amount"))["t"] or 0,
        "approved_count": approved.count(), "year": year,
    })
