from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from campaigns.forms import CampaignSettingsForm
from campaigns.models import Campaign, CampaignRecipient, CampaignSettings, CampaignStatus, OptOut, ReminderLog
from campaigns.service import read_unsubscribe_token, send_test, sms_available, mail_available
from core.tenancy import tenant_context


@login_required
def dashboard(request):
    month = timezone.localdate().replace(day=1)
    month_rows = CampaignRecipient.objects.filter(attempted_at__date__gte=month)
    recent = list(Campaign.objects.select_related("segment").annotate(
        n_total=Count("recipients"), n_sent=Count("recipients", filter=Q(recipients__status="sent")),
        n_failed=Count("recipients", filter=Q(recipients__status="failed")),
        n_skipped=Count("recipients", filter=Q(recipients__status="skipped")))[:10])
    return render(request, "campaigns/dashboard.html", {
        "title": "لوحة حملات التواصل",
        "campaigns_sent": Campaign.objects.filter(status=CampaignStatus.SENT, sent_at__date__gte=month).count(),
        "messages_sent": month_rows.filter(status="sent").count()
        + ReminderLog.objects.filter(created_at__date__gte=month, status="sent").count(),
        "failures": month_rows.filter(status="failed").count(),
        "optouts": OptOut.objects.count(),
        "optouts_month": OptOut.objects.filter(created_at__date__gte=month).count(),
        "scheduled": Campaign.objects.filter(status=CampaignStatus.SCHEDULED).order_by("scheduled_at"),
        "recent": recent, "sms_ok": sms_available(request.company), "mail_ok": mail_available(request.company),
    })


@login_required
def settings_page(request):
    obj = CampaignSettings.for_company(request.company)
    form = CampaignSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ إعدادات التذكير.")
        return redirect("campaigns_settings")
    return render(request, "campaigns/settings.html", {
        "title": "إعدادات التذكير", "form": form, "sms_ok": sms_available(request.company),
        "reminders": ReminderLog.objects.order_by("-id")[:30]})


@login_required
@require_POST
def test_send(request, pk):
    campaign = get_object_or_404(Campaign.objects.all(), pk=pk)
    ok, info = send_test(campaign, request.POST.get("target", ""))
    (messages.success if ok else messages.error)(request, info)
    return redirect(campaign.get_absolute_url())


def unsubscribe(request, token):
    """صفحة عامة: إلغاء استلام رسائل البريد برابط موقّع. التأكيد بزر (POST) حتى لا تُلغيه برامج فحص الروابط."""
    from core.models import Company

    try:
        company_id, email = read_unsubscribe_token(token)
    except signing.BadSignature:
        return render(request, "campaigns/unsubscribe.html", {"invalid": True}, status=400)
    company = get_object_or_404(Company, pk=company_id)
    done = OptOut.all_objects.filter(company=company, email=email).exists()
    if request.method == "POST" and not done:
        with tenant_context(company):
            OptOut.all_objects.create(company=company, email=email, source="link", reason="إلغاء من رابط البريد")
        from website.models import NewsletterSubscriber

        NewsletterSubscriber.all_objects.filter(company=company, email__iexact=email).update(is_active=False)
        done = True
    return render(request, "campaigns/unsubscribe.html", {"company": company, "email": email, "done": done})
