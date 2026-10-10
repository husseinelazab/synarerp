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
from pos.services import checkout, find_returnable_invoice, pos_return
from selling.models import Customer, SalesInvoice
from stock.models import Bin, Item, ItemGroup


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
        "invoiceUrl": reverse("pos_invoice"),
        "returnUrl": reverse("pos_return"),
        "taxRate": float(profile.tax_rate),
        "pricesIncludeTax": profile.prices_include_tax,
        "allowDiscount": profile.allow_discount,
        "maxDiscountPercent": float(profile.max_discount_percent),
        "allowRateChange": profile.allow_rate_change,
        "defaultCustomer": profile.default_customer_id,
        "currencyHtml": currency_symbol_html(profile.company.currency),
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
    })


@login_required
def items_api(request):
    shift = POSShift.current_for(request.user)
    if shift is None:
        return JsonResponse({"error": "لا توجد وردية مفتوحة."}, status=400)
    stock = dict(Bin.objects.filter(warehouse=shift.profile.warehouse).values_list("item_id", "actual_qty"))
    items = [
        {
            "id": i.id, "code": i.code, "name": i.name, "barcode": i.barcode, "group": i.item_group_id,
            "uom": i.uom.name, "rate": float(i.selling_rate), "stock": i.is_stock_item,
            "qty": float(stock.get(i.id, 0)),
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
            paid_cash=data.get("paid_cash") or 0, paid_card=data.get("paid_card") or 0,
        )
    except (ValidationError, Customer.DoesNotExist) as exc:
        return JsonResponse({"error": _error(exc) if isinstance(exc, ValidationError) else "العميل غير موجود."}, status=400)
    return JsonResponse({
        "name": invoice.name,
        "grand_total": str(invoice.grand_total),
        "change": str(invoice.change_amount),
        "receipt_url": reverse("pos_receipt", args=[invoice.pk]),
    })


@login_required
def receipt(request, pk):
    invoice = get_object_or_404(SalesInvoice.objects.select_related("company", "customer", "pos_shift__profile"), pk=pk)
    return render(request, "pos/receipt.html", {
        "inv": invoice, "company": invoice.company, "rows": invoice.items.select_related("item"),
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
    return JsonResponse({
        "name": invoice.name, "customer": str(invoice.customer), "date": invoice.posting_date.isoformat(),
        "grand_total": str(invoice.grand_total), "paid_card": str(invoice.paid_card), "factor": factor,
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
            data.get("items") or [], refund_method=data.get("refund_method") or "cash",
        )
    except ValidationError as exc:
        return JsonResponse({"error": _error(exc)}, status=400)
    return JsonResponse({
        "name": ret.name, "refund": str(-ret.grand_total),
        "refund_cash": str(-ret.paid_cash), "refund_card": str(-ret.paid_card),
        "receipt_url": reverse("pos_receipt", args=[ret.pk]),
    })
