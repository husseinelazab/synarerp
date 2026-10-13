from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from beneficiaries.models import (AidDisbursement, Beneficiary, BeneficiaryStatus, CaseAssessment, EntityRequest,
                                  RequestStatus)


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
        "aid_month": AidDisbursement.objects.filter(docstatus=1, posting_date__gte=today.replace(day=1))
        .aggregate(t=Sum("amount"), n=Count("id")),
        "pending_aid": AidDisbursement.objects.filter(docstatus=0).select_related("beneficiary", "program")[:50],
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


def send_otp_message(company, phone, body, reference, user):
    """الإرسال عبر مزود الرسائل فقط: الرمز لا يُعرض ولا يُرسل برابط واتساب يفتحه الموظف."""
    from messaging.service import send

    send(company, phone, body, event="aid_otp", reference=reference, user=user, mode="provider")


def _aid(request, pk):
    from django.shortcuts import get_object_or_404

    from beneficiaries.models import AidDisbursement

    return get_object_or_404(AidDisbursement.objects.select_related("beneficiary", "program"), pk=pk)


@login_required
@require_POST
def aid_send_otp(request, pk):
    aid = _aid(request, pk)
    try:
        with transaction.atomic():
            code = aid.issue_otp()
            body = (f"رمز تأكيد استلام «{aid.program}» من {request.company.name}: {code}. "
                    "لا تشاركه إلا عند استلامك المساعدة فعلياً.")
            send_otp_message(request.company, aid.beneficiary.phone, body, aid.name, request.user)
    except (ValidationError, ValueError) as exc:
        messages.error(request, exc.messages[0] if isinstance(exc, ValidationError) else str(exc))
    else:
        messages.success(request, f"أُرسل رمز التحقق إلى جوال المستفيد ({aid.beneficiary.phone}). صالح 10 دقائق.")
    return redirect(aid.get_absolute_url())


@login_required
@require_POST
def aid_confirm(request, pk):
    aid = _aid(request, pk)
    manual = request.POST.get("manual") == "1"
    try:
        if manual:
            if not request.membership.can_manage:
                raise ValidationError("التأكيد اليدوي لمالك الحساب والمدير فقط.")
            aid.confirm_manually(request.user, request.POST.get("note", ""))
        else:
            aid.verify_otp(request.POST.get("code", ""), request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, "تم تأكيد استلام المستفيد. يمكنك الآن اعتماد الصرف.")
    return redirect(aid.get_absolute_url())
