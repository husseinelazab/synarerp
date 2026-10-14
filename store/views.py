"""المتجر الإلكتروني من داخل النظام: اللوحة والإعدادات، ونقطة استقبال إشعارات ميسّر (ويب هوك)."""
import hmac
import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.tenancy import tenant_context
from core.utils import flt
from store import moyasar
from store.forms import OpportunityImageForm, StoreSettingsForm
from store.models import (DonationOpportunity, DonationOrder, DonationOrderLine, Marketer, OrderStatus,
                          RecurringDonation, StoreSettings)
from store.service import fetch_and_finalize

log = logging.getLogger(__name__)


@login_required
def store_dashboard(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    paid = DonationOrder.objects.filter(status=OrderStatus.PAID)
    month_orders = DonationOrder.objects.filter(created_at__date__gte=month_start)
    month_total = month_orders.count()
    month_paid = month_orders.filter(status=OrderStatus.PAID).count()
    kpis = [
        {"label": "تبرعات اليوم", "value": flt(paid.filter(paid_at__date=today).aggregate(t=Sum("total"))["t"]),
         "money": True},
        {"label": "تبرعات هذا الشهر",
         "value": flt(paid.filter(paid_at__date__gte=month_start).aggregate(t=Sum("total"))["t"]), "money": True},
        {"label": "الطلبات المدفوعة", "value": paid.count()},
        {"label": "نسبة إتمام الدفع هذا الشهر",
         "value": f"{round(month_paid * 100 / month_total) if month_total else 0}%",
         "hint": f"{month_paid} من {month_total}"},
        {"label": "التبرعات الشهرية النشطة", "value": RecurringDonation.objects.filter(active=True).count()},
    ]
    top = (DonationOrderLine.objects.filter(order__status=OrderStatus.PAID, order__company=request.company)
           .values("opportunity__id", "opportunity__title").annotate(total=Sum("amount"), count=Count("id"))
           .order_by("-total")[:8])
    marketers = (Marketer.objects.annotate(total=Sum("orders__total", filter=Q(orders__status=OrderStatus.PAID)),
                                           count=Count("orders", filter=Q(orders__status=OrderStatus.PAID)))
                 .filter(total__gt=0).order_by("-total")[:8])
    settings = StoreSettings.for_company(request.company)
    from website.models import Website

    site = Website.for_company(request.company)
    return render(request, "store/dashboard.html", {
        "title": "لوحة المتجر الإلكتروني", "kpis": kpis, "top": top, "marketers": marketers, "settings": settings,
        "site": site, "recent": DonationOrder.objects.select_related("marketer")[:15],
        "opportunities_count": DonationOpportunity.objects.filter(is_published=True).count(),
    })


@login_required
def store_settings(request):
    membership = getattr(request, "membership", None)
    if membership is not None and not membership.can_manage:
        messages.error(request, "إعدادات المتجر لمالك الحساب والمدير فقط.")
        return redirect("store_dashboard")
    obj = StoreSettings.for_company(request.company)
    form = StoreSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "حُفظت إعدادات المتجر.")
        return redirect("store_settings")
    from website.models import Website

    return render(request, "store/settings.html", {
        "title": "إعدادات المتجر وبوابة الدفع", "form": form, "obj": obj, "site": Website.for_company(request.company),
        "webhook_url": request.build_absolute_uri("/store/webhook/"),
    })


@login_required
def opportunity_image(request, pk):
    opportunity = get_object_or_404(DonationOpportunity, pk=pk)
    form = OpportunityImageForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data.get("remove"):
            opportunity.image = ""
        if form.cleaned_data.get("image"):
            opportunity.image = form.cleaned_data["image"]
        opportunity.save(update_fields=["image"])
        messages.success(request, "حُفظت صورة الفرصة.")
        return redirect(opportunity.get_absolute_url())
    return render(request, "store/opportunity_image.html", {"title": f"صورة «{opportunity}»", "form": form,
                                                            "o": opportunity})


@csrf_exempt
@require_POST
def webhook(request):
    """إشعار ميسّر: نتحقق من الرمز السري للجمعية، ثم نجلب العملية من ميسّر ولا نعتمد على محتوى الإشعار."""
    try:
        body = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid json"}, status=400)
    data = body.get("data") if isinstance(body, dict) else None
    token = body.get("secret_token") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(token, str) or not token or not data.get("id"):
        return JsonResponse({"error": "bad request"}, status=400)
    payment_id = str(data["id"])[:64]
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    candidates = list(DonationOrder.all_objects.filter(payment_id=payment_id).select_related("company")[:5])
    if not candidates and metadata.get("ref"):
        candidates = list(DonationOrder.all_objects.filter(reference=str(metadata["ref"])[:16])
                          .select_related("company")[:1])
    if not candidates and metadata.get("order"):
        candidates = list(DonationOrder.all_objects.filter(number=str(metadata["order"])[:30])
                          .select_related("company")[:20])
    if not candidates:
        return JsonResponse({"status": "ignored"})
    order = None
    for candidate in candidates:
        secret = StoreSettings.for_company(candidate.company).webhook_secret
        if secret and hmac.compare_digest(secret.encode(), token.encode()):
            order = candidate
            break
    if order is None:
        return JsonResponse({"error": "forbidden"}, status=403)
    if body.get("type") not in ("payment_paid", "payment_failed", "payment_captured"):
        return JsonResponse({"status": "ignored"})
    with tenant_context(order.company):
        try:
            order, changed = fetch_and_finalize(order, payment_id)
        except moyasar.MoyasarError as exc:
            log.warning("تعذر التحقق من عملية ميسّر %s: %s", payment_id, exc)
            return JsonResponse({"error": "gateway unavailable"}, status=502)  # يُعيد ميسّر المحاولة
    return JsonResponse({"status": order.status, "finalized": changed})
