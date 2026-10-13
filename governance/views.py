from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from governance.models import (MAX_EVIDENCE_BYTES, BoardTerm, DecisionStatus, FeeStatus, GovernanceAssessment,
                               GovernanceEvidence, GovernanceIndicator, Meeting, MeetingDecision, Member,
                               MemberStatus, MembershipFee, generate_fees)


@login_required
def members_dashboard(request):
    year = timezone.localdate().year
    active = Member.objects.filter(status=MemberStatus.ACTIVE).select_related("category")
    fees = MembershipFee.objects.filter(year=year)
    decisions = MeetingDecision.objects.filter(status=DecisionStatus.OPEN).select_related("meeting", "responsible")
    return render(request, "governance/members_dashboard.html", {
        "title": "لوحة إدارة الأعضاء المشاركين", "year": year,
        "active_count": active.count(), "voters": sum(1 for m in active if m.can_vote(year)),
        "by_category": active.values("category__name").annotate(n=Count("id")).order_by("-n"),
        "fees_unpaid": fees.filter(status__in=[FeeStatus.PENDING, FeeStatus.PARTIAL]).select_related("member"),
        "fees_generated": fees.exists(),
        "term": BoardTerm.current(),
        "meetings": Meeting.objects.filter(posting_date__year=year),
        "decisions": decisions.order_by("due_date"),
    })


@login_required
@require_POST
def generate_year_fees(request):
    created = generate_fees(timezone.localdate().year)
    messages.success(request, f"أُنشئ {created} اشتراكاً لهذا العام." if created else "اشتراكات هذا العام موجودة مسبقاً.")
    return redirect("members_dashboard")


@login_required
def governance_dashboard(request):
    year = timezone.localdate().year
    indicators = list(GovernanceIndicator.objects.filter(is_active=True).select_related("standard")
                      .annotate(evidence_count=Count("evidence")))
    for indicator in indicators:
        indicator.result = indicator.evaluate(year)
    return render(request, "governance/governance_dashboard.html", {
        "title": "لوحة إدارة الحوكمة", "year": year, "indicators": indicators,
        "assessments": GovernanceAssessment.objects.order_by("-year", "-id")[:5],
        "without_evidence": sum(1 for i in indicators if not i.evidence_count),
    })


@login_required
@require_POST
def upload_evidence(request, pk):
    indicator = get_object_or_404(GovernanceIndicator, pk=pk)
    upload = request.FILES.get("file")
    title = request.POST.get("title", "").strip()
    link = request.POST.get("link", "").strip()
    try:
        if not title:
            raise ValidationError("اكتب عنوان الشاهد.")
        if not upload and not link:
            raise ValidationError("أرفق ملفاً أو أدخل رابطاً.")
        if upload and upload.size > MAX_EVIDENCE_BYTES:
            raise ValidationError("حجم الملف يتجاوز 5 ميجابايت.")
        evidence = GovernanceEvidence(indicator=indicator, title=title, link=link, uploaded_by=request.user,
                                      year=int(request.POST.get("year") or timezone.localdate().year))
        if upload:
            evidence.file_name, evidence.content_type = upload.name[:255], (upload.content_type or "")[:100]
            evidence.content = upload.read()
        evidence.full_clean(exclude=["content"])
        evidence.save()
    except (ValidationError, ValueError) as exc:
        messages.error(request, exc.messages[0] if isinstance(exc, ValidationError) else "سنة غير صحيحة.")
    else:
        messages.success(request, "تمت إضافة الشاهد.")
    return redirect(indicator.get_absolute_url())


@login_required
def download_evidence(request, pk):
    evidence = get_object_or_404(GovernanceEvidence, pk=pk, content__isnull=False)
    response = HttpResponse(bytes(evidence.content), content_type=evidence.content_type or "application/octet-stream")
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{_quote(evidence.file_name)}"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def delete_evidence(request, pk):
    evidence = get_object_or_404(GovernanceEvidence, pk=pk)
    url = evidence.indicator.get_absolute_url()
    if not request.membership.may("delete"):
        messages.error(request, "ليست لديك صلاحية الحذف.")
    else:
        evidence.delete()
        messages.success(request, "تم حذف الشاهد.")
    return redirect(url)


def _quote(name):
    from urllib.parse import quote

    return quote(name or "evidence")
