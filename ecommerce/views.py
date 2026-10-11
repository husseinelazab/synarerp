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
        if connection.platform == "salla" and str(body.get("event", "")).startswith("app."):
            return JsonResponse({"status": _salla_app_event(connection, body)})
        try:
            with transaction.atomic():
                order = importer.receive(connection, body, request.GET.get("event", ""))
        except PermissionDenied as exc:
            return HttpResponseForbidden(str(exc))
    return JsonResponse({"status": order.status if order else "ignored"})


def _salla_app_event(connection, body):
    """تثبيت تطبيق سلة (الوضع السهل) يرسل رموز الوصول للـ Webhook نفسه؛ وإلغاء التثبيت يمسحها."""
    event, data = body.get("event"), body.get("data") or {}
    if connection.store_id and str(body.get("merchant") or "") not in ("", connection.store_id):
        return "ignored"
    if event == "app.store.authorize" and data.get("access_token"):
        connection.set_tokens(data["access_token"], data.get("refresh_token", ""), data.get("expires"))
        if not connection.store_id and body.get("merchant"):
            connection.store_id = str(body["merchant"])
        connection.save()
        return "authorized"
    if event in ("app.uninstalled", "app.store.uninstalled"):
        connection.set_tokens("", "", None)
        connection.token_expires_at = None
        connection.sync_stock = False
        connection.save()
        return "uninstalled"
    return "ignored"


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


@login_required
@require_POST
def store_action(request, pk):
    """إدارة الوصول لواجهة المنصة: حفظ الرموز، اختبار الاتصال، استيراد المنتجات، مزامنة الكميات."""
    from core.permissions import require_submit
    from ecommerce import sync
    from ecommerce.api import ApiError, client

    connection = get_object_or_404(StoreConnection, pk=pk)
    membership = getattr(request, "membership", None)
    if membership is None or not membership.can_manage:
        messages.error(request, "إعدادات الربط للمالك والمدير فقط.")
        return redirect(connection.get_absolute_url())
    action = request.POST.get("action")
    try:
        if action == "credentials":
            fields = {"access": request.POST.get("access_token", "").strip() or None,
                      "refresh": request.POST.get("refresh_token", "").strip() or None,
                      "partner": request.POST.get("partner_token", "").strip() or None,
                      "client_secret": request.POST.get("client_secret", "").strip() or None}
            connection.set_tokens(**fields)
            connection.save()
            messages.success(request, "حُفظت بيانات الوصول (مشفرة).")
        elif action == "clear":
            connection.set_tokens("", "", None, "", "")
            connection.token_expires_at = None
            connection.sync_stock = False
            connection.save()
            messages.success(request, "حُذفت بيانات الوصول.")
        elif action == "test":
            messages.success(request, client(connection).test())
        elif action == "import":
            with transaction.atomic():
                stats = sync.import_products(connection, create_missing=bool(request.POST.get("create_missing")))
            messages.success(request, f"منتجات المتجر: {stats['total']} — مربوطة {stats['linked']} "
                                      f"(منها أصناف جديدة {stats['created']}) — غير مربوطة {stats['unlinked']}.")
        elif action == "sync":
            require_submit(request)
            count = sync.sync_stock(connection, force=bool(request.POST.get("force")))
            messages.success(request, f"أُرسلت كميات {count} منتج للمتجر." if count else "لا تغييرات في الكميات.")
    except (ApiError, ValidationError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    return redirect(request.POST.get("next") if request.POST.get("next", "").startswith("/ecommerce/")
                    else connection.get_absolute_url())


@login_required
def store_products(request, pk):
    from stock.models import Item

    connection = get_object_or_404(StoreConnection, pk=pk)
    if request.method == "POST":
        product = get_object_or_404(connection.products, pk=request.POST.get("product"))
        item = Item.objects.filter(pk=request.POST.get("item") or 0).first()
        product.item = item
        product.last_pushed_qty = None
        product.save(update_fields=["item", "last_pushed_qty", "updated_at"])
        if item and product.sku:
            SkuMapping.objects.update_or_create(connection=connection, sku=product.sku,
                                                defaults={"item": item, "company": connection.company})
        messages.success(request, f"رُبط {product.sku or product.name} بـ {item}." if item else "أُلغي الربط.")
        return redirect(request.get_full_path())
    from ecommerce.sync import available_qty

    qs = connection.products.select_related("item")
    if request.GET.get("unlinked"):
        qs = qs.filter(item__isnull=True)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    rows = [{"p": p, "available": available_qty(connection, p.item) if p.item_id else None} for p in page]
    return render(request, "ecommerce/products.html", {
        "title": f"منتجات {connection.name}", "connection": connection, "page": page, "rows": rows,
        "items": Item.objects.filter(disabled=False).only("id", "code", "name")[:3000],
        "unlinked": connection.products.filter(item__isnull=True).count(),
    })
