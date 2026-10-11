import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.utils import currency_symbol_html
from core.zatca import invoice_qr_svg
from pos.models import POSProfile, POSShift
from pos.services import checkout, find_returnable_invoice, pos_return, pos_price_list, quote
from selling.models import Customer, SalesInvoice
from stock.models import Bin, Item, ItemGroup, ScaleSettings


def _error(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required
def terminal(request):
    shift = POSShift.current_for(request.user)
    if shift is None:
        return render(request, "pos/open_shift.html", {"profiles": POSProfile.objects.filter(disabled=False)})
    profile = shift.profile
    config = {
        "itemsUrl": reverse("pos_items"),
        "checkoutUrl": reverse("pos_checkout"),
        "quoteUrl": reverse("pos_quote"),
        "invoiceUrl": reverse("pos_invoice"),
        "returnUrl": reverse("pos_return"),
        "taxRate": float(profile.tax_rate),
        "pricesIncludeTax": profile.prices_include_tax,
        "allowDiscount": profile.allow_discount,
        "maxDiscountPercent": float(profile.max_discount_percent),
        "allowRateChange": profile.allow_rate_change,
        "defaultCustomer": profile.default_customer_id,
        "currencyHtml": currency_symbol_html(profile.company.currency),
        "syncUrl": reverse("pos_sync"),
        "scale": ScaleSettings.for_company(profile.company).config(),
        "terminalMode": profile.card_terminal_mode,
        "bridgeUrl": profile.bridge_url.rstrip("/"),
        "terminalId": profile.terminal_id,
        "requireCardRef": profile.require_card_reference,
        "swUrl": reverse("pos_sw"),
        "queueKey": f"synar-pos-queue-{profile.company_id}-{request.user.pk}",
        "companyName": profile.company.name,
        "taxId": profile.company.tax_id,
        "cashier": request.user.get_full_name() or request.user.username,
    }
    return render(request, "pos/terminal.html", {
        "shift": shift, "profile": profile, "config": config,
        "customers": Customer.objects.filter(disabled=False),
        "groups": ItemGroup.objects.all(),
    })


@login_required
@require_POST
def open_shift(request):
    profile = get_object_or_404(POSProfile, pk=request.POST.get("profile"))
    try:
        POSShift.open(profile, request.user, request.POST.get("opening_cash") or 0)
    except ValidationError as exc:
        messages.error(request, _error(exc))
    return redirect("pos_terminal")


@login_required
def close_shift(request):
    shift = POSShift.current_for(request.user)
    if shift is None:
        messages.info(request, "لا توجد وردية مفتوحة.")
        return redirect("pos_terminal")
    if request.method == "POST":
        try:
            shift.close(request.POST.get("closing_cash") or 0, request.POST.get("notes", ""))
        except ValidationError as exc:
            messages.error(request, _error(exc))
        else:
            messages.success(request, "تم إغلاق الوردية.")
            return redirect("pos_shift_report", pk=shift.pk)
    return render(request, "pos/close_shift.html", {"shift": shift, "summary": shift.summary()})


@login_required
def shift_list(request):
    shifts = POSShift.objects.select_related("profile", "user")
    if not request.user.is_staff:
        shifts = shifts.filter(user=request.user)
    return render(request, "pos/shift_list.html", {"shifts": shifts[:200]})


@login_required
def shift_report(request, pk):
    shift = get_object_or_404(POSShift.objects.select_related("profile", "user"), pk=pk)
    if not request.user.is_staff and shift.user_id != request.user.id:
        return redirect("pos_shifts")
    invoices = shift.invoices.select_related("customer").order_by("id")
    top_items = (
        shift.invoices.filter(docstatus=1).values("items__item__name")
        .annotate(qty=Sum("items__qty"), amount=Sum("items__amount")).order_by("-amount")[:10]
    )
    return render(request, "pos/shift_report.html", {
        "shift": shift, "summary": shift.summary(), "invoices": invoices, "top_items": top_items,
        "card_transactions": shift.card_transactions.select_related("invoice").order_by("id"),
    })


@login_required
def items_api(request):
    shift = POSShift.current_for(request.user)
    if shift is None:
        return JsonResponse({"error": "لا توجد وردية مفتوحة."}, status=400)
    stock = dict(Bin.objects.filter(warehouse=shift.profile.warehouse).values_list("item_id", "actual_qty"))
    import datetime

    from django.db.models import Q

    price_list = pos_price_list(shift.profile, shift.profile.default_customer)
    prices = {}  # (الصنف، الوحدة) ← سعر القائمة
    if price_list is not None:
        from pricing.models import ItemPrice

        today = datetime.date.today()
        for p in (ItemPrice.objects.filter(price_list=price_list)
                  .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=today),
                          Q(valid_upto__isnull=True) | Q(valid_upto__gte=today)).select_related("item")
                  .order_by("valid_from")):
            prices[(p.item_id, None if p.uom_id in (None, p.item.uom_id) else p.uom_id)] = float(p.rate)

    def unit_rate(item, unit):
        if (item.id, unit.uom_id) in prices:
            return prices[(item.id, unit.uom_id)]
        if (item.id, None) in prices:
            return round(prices[(item.id, None)] * float(unit.factor), 2)
        return float(unit.effective_selling_rate)
    from stock.models import ItemUOM

    units = {}
    for u in ItemUOM.objects.select_related("uom", "item").order_by("factor"):
        units.setdefault(u.item_id, []).append(u)
    items = [
        {
            "id": i.id, "code": i.code, "name": i.name, "barcode": i.barcode, "group": i.item_group_id,
            "uom": i.uom.name, "stock": i.is_stock_item,
            "rate": prices.get((i.id, None), float(i.selling_rate)),
            "qty": float(stock.get(i.id, 0)), "plu": i.plu if i.is_weighed else None,
            "zero_vat": i.vat_category in ("Z", "E", "O"),
            # وحدات التحويل (كرتون…) بباركودها وسعرها لقراءتها في الكاشير حتى دون اتصال
            "units": [{"uom": u.uom_id, "name": u.uom.name, "factor": float(u.factor), "barcode": u.barcode,
                       "rate": unit_rate(i, u)} for u in units.get(i.id, [])],
        }
        for i in Item.objects.filter(disabled=False).select_related("uom")
    ]
    return JsonResponse({"items": items})


@login_required
@require_POST
def checkout_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة."}, status=400)
    try:
        invoice = checkout(
            POSShift.current_for(request.user), request.user, data.get("items") or [],
            customer_id=data.get("customer"), discount=data.get("discount") or 0,
            paid_cash=data.get("paid_cash") or 0, paid_card=data.get("paid_card") or 0, card=data.get("card"),
            coupon_code=data.get("coupon") or "", loyalty_points=_int(data.get("loyalty_points")),
        )
    except (ValidationError, Customer.DoesNotExist) as exc:
        return JsonResponse({"error": _error(exc) if isinstance(exc, ValidationError) else "العميل غير موجود."}, status=400)
    return JsonResponse({
        "name": invoice.name,
        "grand_total": str(invoice.grand_total),
        "change": str(invoice.change_amount),
        "receipt_url": reverse("pos_receipt", args=[invoice.pk]),
    })


def _int(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


@login_required
@require_POST
def quote_api(request):
    """تسعير السلة بالعروض والكوبون ونقاط الولاء قبل الدفع، دون إنشاء فاتورة."""
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة."}, status=400)
    try:
        result = quote(POSShift.current_for(request.user), data.get("items") or [], customer_id=data.get("customer"),
                       discount=data.get("discount") or 0, coupon_code=data.get("coupon") or "",
                       loyalty_points=_int(data.get("loyalty_points")))
    except (ValidationError, Customer.DoesNotExist) as exc:
        return JsonResponse({"error": _error(exc) if isinstance(exc, ValidationError) else "العميل غير موجود."}, status=400)
    return JsonResponse(result)


@login_required
def receipt(request, pk):
    invoice = get_object_or_404(SalesInvoice.objects.select_related("company", "customer", "pos_shift__profile"), pk=pk)
    return render(request, "pos/receipt.html", {
        "inv": invoice, "company": invoice.company, "rows": invoice.items.select_related("item", "uom"),
        "qr": invoice_qr_svg(invoice), "auto_print": request.GET.get("print") == "1",
        "footer": invoice.pos_shift.profile.receipt_footer if invoice.pos_shift_id else "",
    })


@login_required
def invoice_api(request):
    try:
        invoice = find_returnable_invoice(request.GET.get("name", ""))
    except ValidationError as exc:
        return JsonResponse({"error": _error(exc)}, status=404)
    remaining = invoice.returnable_quantities()
    if not remaining:
        return JsonResponse({"error": "تم إرجاع كل أصناف هذه الفاتورة مسبقاً."}, status=400)
    summary = invoice.item_summary()
    items = Item.objects.in_bulk(list(remaining))
    # معامل تحويل مبلغ الأصناف إلى المبلغ المستحق بعد الخصم والضريبة
    factor = float(invoice.grand_total / invoice.total) if invoice.total else 0
    card_tx = invoice.card_transactions.filter(kind="sale").first()
    return JsonResponse({
        "name": invoice.name, "customer": str(invoice.customer), "date": invoice.posting_date.isoformat(),
        "grand_total": str(invoice.grand_total), "paid_card": str(invoice.paid_card), "factor": factor,
        # مرجع عملية البطاقة الأصلية لإرسال الاسترداد لنفس العملية عبر جهاز مدى
        "card": {"rrn": card_tx.rrn, "auth_code": card_tx.auth_code} if card_tx else None,
        "items": [
            {"item": item_id, "name": items[item_id].name, "sold": float(summary[item_id]["qty"]),
             "returnable": float(qty), "rate": float(summary[item_id]["amount"] / summary[item_id]["qty"])}
            for item_id, qty in remaining.items()
        ],
    })


@login_required
@require_POST
def return_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة."}, status=400)
    try:
        ret = pos_return(
            POSShift.current_for(request.user), request.user, data.get("invoice") or "",
            data.get("items") or [], refund_method=data.get("refund_method") or "cash", card=data.get("card"),
        )
    except ValidationError as exc:
        return JsonResponse({"error": _error(exc)}, status=400)
    return JsonResponse({
        "name": ret.name, "refund": str(-ret.grand_total),
        "refund_cash": str(-ret.paid_cash), "refund_card": str(-ret.paid_card),
        "receipt_url": reverse("pos_receipt", args=[ret.pk]),
    })


@login_required
@require_POST
def sync_api(request):
    """مزامنة مبيعات تمت دون اتصال: كل بيع مستقل، والمكرر (نفس المعرّف) يعيد فاتورته دون إنشاء جديدة."""
    from django.utils.dateparse import parse_datetime

    try:
        data = json.loads(request.body or "{}")
        sales = data.get("sales") or []
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "بيانات غير صالحة."}, status=400)
    shift = POSShift.current_for(request.user)
    if shift is None:
        return JsonResponse({"error": "افتح وردية لمزامنة المبيعات المعلقة."}, status=409)
    results = []
    for sale in sales[:200]:
        client_id = str(sale.get("client_id") or "")[:64]
        if not client_id:
            results.append({"client_id": "", "ok": False, "error": "معرّف البيع مفقود."})
            continue
        created = parse_datetime(sale.get("created_at") or "") if sale.get("created_at") else None
        try:
            invoice = checkout(
                shift, request.user, sale.get("items") or [], customer_id=sale.get("customer"),
                discount=sale.get("discount") or 0, paid_cash=sale.get("paid_cash") or 0,
                paid_card=sale.get("paid_card") or 0, offline_id=client_id, offline_at=created, card=sale.get("card"),
                apply_rules=False,
            )
        except (ValidationError, Customer.DoesNotExist) as exc:
            results.append({"client_id": client_id, "ok": False,
                            "error": _error(exc) if isinstance(exc, ValidationError) else "العميل غير موجود."})
            continue
        results.append({"client_id": client_id, "ok": True, "name": invoice.name,
                        "receipt_url": reverse("pos_receipt", args=[invoice.pk])})
    return JsonResponse({"results": results})


def service_worker(request):
    """عامل الخدمة لنقطة البيع (نطاقه /pos/) — يخزن الشاشة والأصناف للعمل دون اتصال."""
    from django.conf import settings
    from django.http import HttpResponse
    from django.template.loader import render_to_string
    from django.templatetags.static import static

    assets = [static(path) for path in ("css/erp.css", "css/currency.css", "css/pos.css", "js/pos.js",
                                         "fonts/saudi-riyal/saudi_riyal.woff2", "fonts/saudi-riyal/saudi_riyal_bold.woff2")]
    body = render_to_string("pos/sw.js", {"assets": assets, "version": settings.SYNAR_POS_CACHE_VERSION})
    response = HttpResponse(body, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = "/pos/"
    return response
