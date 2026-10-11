import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.tenancy import tenant_context
from ecommerce import importer
from ecommerce.models import ExternalOrder, OrderStatus, SkuMapping, StoreConnection
from ecommerce.platforms import verify

MAX_BODY = 1024 * 1024


@csrf_exempt
@require_POST
def webhook(request, platform, key):
    """نقطة استقبال طلبات المتجر. الرابط سري لكل متجر، ويُتحقق من التوقيع إن ضُبط السر."""
    connection = StoreConnection.all_objects.filter(webhook_key=key, platform=platform).select_related("company").first()
    if connection is None:
        return HttpResponse(status=404)
    raw = request.body
    if len(raw) > MAX_BODY:
        return HttpResponse(status=413)
    if not verify(connection, request, raw):
        return HttpResponseForbidden("invalid signature")
    if not connection.enabled:
        return JsonResponse({"status": "disabled"})
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("invalid json")
    if not isinstance(body, dict):
        return HttpResponseBadRequest("invalid payload")
    with tenant_context(connection.company):
        try:
            with transaction.atomic():
                order = importer.receive(connection, body, request.GET.get("event", ""))
        except PermissionDenied as exc:
            return HttpResponseForbidden(str(exc))
    return JsonResponse({"status": order.status if order else "ignored"})


@login_required
def orders(request):
    qs = ExternalOrder.objects.select_related("connection", "invoice")
    status = request.GET.get("status", "")
    if status in OrderStatus.values:
        qs = qs.filter(status=status)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "ecommerce/orders.html", {
        "title": "طلبات المتاجر الإلكترونية", "page": page, "status": status, "statuses": OrderStatus.choices,
        "review_count": ExternalOrder.objects.filter(status=OrderStatus.REVIEW).count(),
    })


@login_required
def order_detail(request, pk):
    from stock.models import Item

    order = get_object_or_404(ExternalOrder.objects.select_related("connection", "invoice", "payment"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            with transaction.atomic():
                if action == "map":
                    item = get_object_or_404(Item, pk=request.POST.get("item"))
                    SkuMapping.objects.update_or_create(connection=order.connection, sku=request.POST.get("sku", "")[:100],
                                                        defaults={"item": item, "company": order.company})
                    importer.retry(order)
                elif action == "retry":
                    importer.retry(order)
                elif action == "ignore":
                    order.status = OrderStatus.IGNORED
                    order.save(update_fields=["status", "updated_at"])
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            order.refresh_from_db()
            if order.status == OrderStatus.IMPORTED:
                messages.success(request, f"تم: الفاتورة {order.invoice.name}.")
            elif order.status == OrderStatus.REVIEW:
                messages.warning(request, order.error)
        return redirect(order.get_absolute_url())
    data = importer.NORMALIZERS[order.connection.platform](order.payload)
    missing = [line for line in data["items"]
               if importer.resolve_item(order.connection, line["sku"], line["name"])[0] is None]
    return render(request, "ecommerce/order.html", {
        "title": str(order), "order": order, "data": data, "missing": missing, "order_date": importer.order_date(data["date"]),
        "items": Item.objects.filter(disabled=False).only("id", "code", "name")[:2000],
        "payload": json.dumps(order.payload, ensure_ascii=False, indent=2),
    })


@login_required
@require_POST
def test_payload(request, pk):
    """تجربة الربط بلصق حمولة طلب حقيقية من المتجر (من سجل الـ Webhooks في لوحة المنصة)."""
    connection = get_object_or_404(StoreConnection, pk=pk)
    try:
        body = json.loads(request.POST.get("payload") or "")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        messages.error(request, "النص ليس JSON صحيحاً لطلب واحد.")
        return redirect(connection.get_absolute_url())
    try:
        with transaction.atomic():
            order = importer.receive(connection, body, request.POST.get("event", ""))
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect(connection.get_absolute_url())
    if order is None:
        messages.info(request, "الحمولة ليست طلباً (أو بلا معرّف طلب)؛ تُقبل وتُتجاهل.")
        return redirect(connection.get_absolute_url())
    return redirect(order.get_absolute_url())
