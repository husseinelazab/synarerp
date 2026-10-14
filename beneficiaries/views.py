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


# ───────────────────────────── لجنة المساعدات ─────────────────────────────

def _committee_document(slug, pk):
    from django.http import Http404
    from django.shortcuts import get_object_or_404

    from beneficiaries.committee import ASSESSMENT, DISBURSEMENT
    from core.registry import get_doctype

    if slug not in (ASSESSMENT, DISBURSEMENT):
        raise Http404
    dt = get_doctype(slug)
    return get_object_or_404(dt.model.objects.all(), pk=pk)


@login_required
@require_POST
def committee_refer(request, slug, pk):
    from beneficiaries.committee import refer

    doc = _committee_document(slug, pk)
    try:
        case = refer(doc, request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, f"عُرض على {case.committee} ونُبّه الأعضاء للتصويت.")
    return redirect(doc.get_absolute_url())


@login_required
@require_POST
def committee_vote(request, pk):
    from django.shortcuts import get_object_or_404

    from beneficiaries.committee import document_of, vote
    from beneficiaries.models import CommitteeCase

    case = get_object_or_404(CommitteeCase.objects.select_related("committee"), pk=pk)
    try:
        vote(case, request.user, request.POST.get("vote", ""), request.POST.get("note", ""))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        case.refresh_from_db()
        messages.success(request, "سُجّل صوتك." + (f" {case.get_status_display()}." if case.status != "pending" else ""))
    back = request.POST.get("next", "")
    if back == "committee":
        return redirect("committee_page")
    doc = document_of(case)
    return redirect(doc.get_absolute_url() if doc else "committee_page")


@login_required
def committee_page(request):
    from beneficiaries.committee import pending_for, tally
    from beneficiaries.models import AidCommittee, CaseStatus, CommitteeCase

    committee = AidCommittee.objects.first()
    mine = list(pending_for(request.user, request.company))
    pending = list(CommitteeCase.objects.filter(status=CaseStatus.PENDING).select_related("committee", "referred_by"))
    decided = list(CommitteeCase.objects.exclude(status=CaseStatus.PENDING).select_related("committee")[:50])
    for case in mine + pending + decided:
        case.approve_count, case.reject_count = tally(case)
    return render(request, "beneficiaries/committee.html", {
        "title": "قرارات لجنة المساعدات", "committee": committee, "mine": mine, "pending": pending,
        "decided": decided,
        "is_member": bool(committee and committee.members.filter(pk=request.user.pk).exists()),
    })


@login_required
def committee_minutes(request, pk):
    """محضر قرار اللجنة للطباعة: الموضوع، والأصوات، والقرار."""
    from django.shortcuts import get_object_or_404

    from beneficiaries.committee import document_of, tally
    from beneficiaries.models import CommitteeCase
    from core.hijri import format_hijri
    from core.models import PrintSettings

    case = get_object_or_404(CommitteeCase.objects.select_related("committee", "referred_by"), pk=pk)
    approve, reject = tally(case)
    return render(request, "beneficiaries/committee_minutes.html", {
        "case": case, "doc": document_of(case), "votes": case.votes.select_related("user"),
        "approve": approve, "reject": reject, "company": request.company,
        "ps": PrintSettings.for_company(request.company),
        "hijri": format_hijri((case.decided_at or case.referred_at).date()),
    })


# ───────────────────────────── القسائم الشرائية ─────────────────────────────

@login_required
@require_POST
def voucher_unlock(request, pk):
    from django.shortcuts import get_object_or_404

    from beneficiaries.models import AidVoucher

    voucher = get_object_or_404(AidVoucher, pk=pk)
    if not request.membership.can_manage:
        messages.error(request, "فك قفل القسيمة لمالك الحساب والمدير فقط.")
    else:
        voucher.failed_attempts = 0
        voucher.save(update_fields=["failed_attempts"])
        messages.success(request, f"فُك قفل {voucher}.")
    return redirect("doc_detail", slug="aid-voucher", pk=voucher.pk)


@login_required
@require_POST
def voucher_resend(request, pk):
    from django.shortcuts import get_object_or_404

    from beneficiaries.models import AidVoucher, VoucherStatus
    from beneficiaries.vouchers import send_voucher_message

    voucher = get_object_or_404(AidVoucher.objects.select_related("beneficiary"), pk=pk)
    if voucher.status != VoucherStatus.ACTIVE:
        messages.error(request, f"القسيمة {voucher.get_status_display()}.")
    elif send_voucher_message(voucher):
        messages.success(request, f"أُرسلت رسالة القسيمة إلى {voucher.beneficiary.phone}.")
    else:
        messages.error(request, "مزوّد الرسائل غير مفعّل. فعّله من الإعدادات ← رسائل SMS وواتساب، أو اطبع القسيمة.")
    return redirect("doc_detail", slug="aid-voucher", pk=voucher.pk)


@login_required
@require_POST
def merchant_regenerate(request, pk):
    from django.shortcuts import get_object_or_404

    from beneficiaries.models import VoucherMerchant

    merchant = get_object_or_404(VoucherMerchant, pk=pk)
    if not request.membership.can_manage:
        messages.error(request, "تغيير رابط المتجر لمالك الحساب والمدير فقط.")
    else:
        merchant.regenerate_token()
        messages.success(request, "أُنشئ رابط جديد للمتجر وأُبطل الرابط القديم. أرسل الرابط الجديد للمتجر.")
    return redirect("doc_detail", slug="voucher-merchant", pk=merchant.pk)


MERCHANT_FAILURES_PER_HOUR = 30


def merchant_portal(request, token):
    """صفحة المتجر الشريك (رابط سري بلا تسجيل دخول): يتحقق من القسيمة ويخصم قيمة المشتريات منها."""
    from django.core.cache import cache
    from django.http import Http404

    from beneficiaries import vouchers
    from beneficiaries.models import VoucherMerchant
    from core.tenancy import tenant_context

    merchant = VoucherMerchant.all_objects.select_related("company", "supplier").filter(token=token).first()
    if merchant is None or not merchant.is_active:
        raise Http404
    context = {"merchant": merchant, "company": merchant.company}
    limit_key = f"voucher-merchant-failures:{merchant.pk}"
    with tenant_context(merchant.company):
        if request.method == "POST":
            action = request.POST.get("action", "check")
            context["form"] = {k: request.POST.get(k, "") for k in ("code", "id_last4", "amount", "invoice_no")}
            if (cache.get(limit_key) or 0) >= MERCHANT_FAILURES_PER_HOUR:
                context["error"] = "تجاوزت عدد المحاولات الخاطئة. حاول بعد ساعة."
            else:
                try:
                    voucher = vouchers.find_for_merchant(merchant, request.POST.get("code"),
                                                         request.POST.get("id_last4"))
                    context["voucher"] = voucher
                    if action == "redeem":
                        from decimal import Decimal, InvalidOperation

                        try:
                            amount = Decimal(request.POST.get("amount", "").replace(",", "").strip())
                        except InvalidOperation:
                            raise ValidationError("أدخل مبلغ المشتريات.")
                        redemption = vouchers.redeem(merchant, voucher, amount, request.POST.get("invoice_no", ""))
                        voucher.refresh_from_db()
                        context.update(done=redemption, voucher=voucher, form={})
                except ValidationError as exc:
                    if "رقم القسيمة" in exc.messages[0] or "الهوية" in exc.messages[0]:
                        if cache.add(limit_key, 1, 3600) is False:
                            cache.incr(limit_key)
                    context["error"] = exc.messages[0]
        context["recent"] = list(merchant.redemptions.filter(docstatus=1).select_related("voucher")
                                 .order_by("-id")[:15])
        if context.get("voucher"):
            allowed = vouchers.allowed_merchants(context["voucher"].disbursement.program)
            context["allowed_here"] = merchant in allowed
    return render(request, "beneficiaries/merchant_portal.html", context)
