"""أدوات الباركود: الجرد بالماسح الضوئي، فاحص الأسعار، وطباعة ملصقات الأصناف والرفوف والأسعار."""
import json
import math
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.utils import flt
from stock import barcode
from stock.models import Bin, Item, ItemGroup, StockCount

# ---------------- أسعار الملصقات ----------------


def prices_include_tax_default():
    """أسعار الأصناف في النظام شاملة الضريبة إن كانت نقطة البيع تعمل بأسعار شاملة."""
    from pos.models import POSProfile

    profile = POSProfile.objects.filter(disabled=False).first()
    return profile.prices_include_tax if profile else False


def shelf_price(item, price, includes_tax, company):
    """السعر للعميل شاملاً الضريبة (النظام يُلزم بعرض السعر النهائي)."""
    price = flt(price)
    if includes_tax or item.vat_category in ("Z", "E", "O"):
        return price
    return flt(price * (100 + flt(company.default_tax_rate)) / 100)


def item_json(item, company, includes_tax=None):
    includes_tax = prices_include_tax_default() if includes_tax is None else includes_tax
    return {"id": item.id, "code": item.code, "name": item.name, "barcode": item.barcode, "uom": str(item.uom),
            "price": str(shelf_price(item, item.selling_rate, includes_tax, company)),
            "shelf": item.shelf_location, "group": str(item.item_group), "rate": str(item.selling_rate),
            "zero_vat": item.vat_category in ("Z", "E", "O")}


def _error(exc):
    return "؛ ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)


# ---------------- البحث بالكود (للماسح في كل الشاشات) ----------------

@login_required
def lookup_api(request):
    item = Item.find_by_code(request.GET.get("code", ""))
    if item is None:
        return JsonResponse({"error": "لا يوجد صنف بهذا الباركود أو الكود."}, status=404)
    data = item_json(item, request.company)
    data.update(selling_rate=str(item.selling_rate), buying_rate=str(item.buying_rate))
    if request.GET.get("stock"):
        bins = Bin.objects.filter(item=item).select_related("warehouse").order_by("warehouse__name")
        data["stock"] = [{"warehouse": b.warehouse.name, "qty": float(b.actual_qty)} for b in bins if b.actual_qty]
    return JsonResponse(data)


@login_required
def items_api(request):
    """أصناف لقائمة الملصقات: بحث نصي، أو مجموعة، أو أسطر مستند شراء/استلام بكمياتها وتواريخ صلاحيتها."""
    company = request.company
    includes_tax = request.GET["inc"] == "1" if "inc" in request.GET else prices_include_tax_default()
    doc = request.GET.get("doc", "")
    if doc:
        rows = _document_rows(doc)
        if rows is None:
            return JsonResponse({"error": "المستند غير موجود."}, status=404)
        return JsonResponse({"items": [
            dict(item_json(r.item, company, includes_tax), copies=max(1, math.ceil(abs(r.qty))),
                 batch=getattr(r, "batch_no", ""),
                 expiry=r.expiry_date.isoformat() if getattr(r, "expiry_date", None) else "")
            for r in rows if r.item.is_stock_item]})
    qs = Item.objects.filter(disabled=False).select_related("uom", "item_group")
    if request.GET.get("id"):
        qs = qs.filter(pk=request.GET["id"]) if request.GET["id"].isdigit() else qs.none()
    elif request.GET.get("group"):
        qs = qs.filter(item_group_id=request.GET["group"])
    elif request.GET.get("q"):
        q = request.GET["q"].strip()
        qs = qs.filter(name__icontains=q) | qs.filter(code__icontains=q) | qs.filter(barcode=q)
    else:
        return JsonResponse({"items": []})
    return JsonResponse({"items": [item_json(i, company, includes_tax) for i in qs.order_by("code")[:300]]})


def _document_rows(doc):
    from buying.models import PurchaseInvoice, PurchaseReceipt

    kind, _, pk = doc.partition("-")
    model = {"pr": PurchaseReceipt, "pinv": PurchaseInvoice}.get(kind)
    if model is None or not pk.isdigit():
        return None
    obj = model.objects.filter(pk=pk, docstatus=1).first()
    return list(obj.items.select_related("item", "item__uom", "item__item_group").order_by("id")) if obj else None


# ---------------- الجرد بالماسح ----------------

def _count(request, pk):
    return get_object_or_404(StockCount.objects.select_related("warehouse", "item_group"), pk=pk)


@login_required
def count_scan(request, pk):
    count = _count(request, pk)
    if not count.is_draft:
        messages.error(request, "الجرد معتمد أو ملغى؛ المسح متاح للمسودات فقط.")
        return redirect("doc_detail", slug="stock-count", pk=count.pk)
    return render(request, "stock/count_scan.html", {"count": count})


def _totals_payload(count):
    rows = count.scanned_totals()
    return {"rows": [{"item": r["item_id"], "code": r["item__code"], "name": r["item__name"],
                      "barcode": r["item__barcode"], "qty": float(r["qty"]), "reads": r["reads"]} for r in rows],
            "reads": count.scans.count()}


@login_required
def count_scan_api(request, pk):
    count = _count(request, pk)
    if request.method == "GET":
        return JsonResponse(_totals_payload(count))
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة."}, status=400)
    action = data.get("action", "scan")
    try:
        with transaction.atomic():
            if action == "undo":
                last = count.scans.filter(user=request.user).order_by("-id").first()
                if last is None:
                    raise ValidationError("لا توجد قراءة لك للتراجع عنها.")
                if not count.is_draft:
                    raise ValidationError("الجرد معتمد أو ملغى.")
                result = {"undone": {"name": last.item.name, "qty": float(last.qty)}}
                last.delete()
            elif action == "set":
                # تصحيح الإجمالي يدوياً: تُسجَّل قراءة بالفرق حتى يبقى السجل كاملاً
                item = get_object_or_404(Item, pk=data.get("item"))
                current = flt(count.scans.filter(item=item).aggregate(t=Sum("qty"))["t"] or 0, 3)
                target = flt(Decimal(str(data.get("qty"))), 3)
                if target < 0:
                    raise ValidationError("الكمية لا تكون سالبة.")
                if target == current:
                    result = {"item": {"name": item.name}, "total": float(current)}
                else:
                    item, total = count.scan(item.barcode or item.code, target - current, request.user)
                    result = {"item": {"id": item.id, "name": item.name}, "total": float(total)}
            else:
                qty = Decimal(str(data.get("qty") or 1))
                item, total = count.scan(data.get("code", ""), qty, request.user)
                result = {"item": {"id": item.id, "name": item.name, "code": item.code}, "total": float(total),
                          "qty": float(qty)}
    except (ValidationError, InvalidOperation, ValueError) as exc:
        return JsonResponse({"error": _error(exc) if isinstance(exc, ValidationError) else "كمية غير صالحة."},
                            status=400)
    result.update(_totals_payload(count))
    return JsonResponse(result)


@login_required
@require_POST
def count_apply(request, pk):
    count = _count(request, pk)
    try:
        n = count.apply_scans(zero_unscanned=bool(request.POST.get("zero_unscanned")))
        messages.success(request, f"نُقلت نتائج المسح ({n} صنف) إلى الجرد. راجع الفروقات ثم اعتمد الجرد.")
    except ValidationError as exc:
        messages.error(request, _error(exc))
        return redirect("stock_count_scan", pk=count.pk)
    return redirect("doc_detail", slug="stock-count", pk=count.pk)


# ---------------- فاحص الأسعار ----------------

@login_required
def price_checker(request):
    from core.utils import currency_symbol_html

    return render(request, "stock/price_checker.html", {"currency_html": currency_symbol_html(request.company.currency)})


# ---------------- الملصقات ----------------

LABEL_SIZES = {
    # حراري: ملصق واحد في كل صفحة بمقاسه
    "38x25": {"label": "حراري 38×25 مم", "w": 38, "h": 25},
    "40x30": {"label": "حراري 40×30 مم", "w": 40, "h": 30},
    "50x25": {"label": "حراري 50×25 مم", "w": 50, "h": 25},
    "50x30": {"label": "حراري 50×30 مم", "w": 50, "h": 30},
    "58x40": {"label": "حراري 58×40 مم", "w": 58, "h": 40},
    "60x40": {"label": "حراري 60×40 مم", "w": 60, "h": 40},
    "100x50": {"label": "حراري 100×50 مم (ملصق رف كبير)", "w": 100, "h": 50},
    # أوراق A4 جاهزة القص
    "a4-24": {"label": "ورق A4 — 24 ملصق (70×37 مم)", "w": 70, "h": 37, "cols": 3, "rows": 8,
              "top": 0.5, "side": 0, "gap_x": 0, "gap_y": 0},
    "a4-40": {"label": "ورق A4 — 40 ملصق (52.5×29.7 مم)", "w": 52.5, "h": 29.7, "cols": 4, "rows": 10,
              "top": 0, "side": 0, "gap_x": 0, "gap_y": 0},
    "a4-65": {"label": "ورق A4 — 65 ملصق (38.1×21.2 مم)", "w": 38.1, "h": 21.2, "cols": 5, "rows": 13,
              "top": 10.7, "side": 4.75, "gap_x": 2.5, "gap_y": 0},
}
LABEL_TYPES = {
    "item": "ملصق صنف (باركود + سعر)",
    "shelf": "ملصق رف (سعر كبير + باركود + موقع الرف)",
    "price": "بطاقة سعر (اسم + سعر)",
    "barcode": "باركود فقط",
}
MAX_LABELS = 3000


@login_required
def labels(request):
    from buying.models import PurchaseInvoice, PurchaseReceipt

    if request.method == "POST":
        return _print_labels(request)
    docs = ([("pr-%d" % d.pk, f"{d.name} — إذن استلام — {d.supplier}") for d in
             PurchaseReceipt.objects.filter(docstatus=1).select_related("supplier").order_by("-id")[:30]]
            + [("pinv-%d" % d.pk, f"{d.name} — فاتورة مشتريات — {d.supplier}") for d in
               PurchaseInvoice.objects.filter(docstatus=1, is_return=False, update_stock=True)
               .select_related("supplier").order_by("-id")[:30]])
    return render(request, "stock/labels.html", {
        "sizes": LABEL_SIZES, "types": LABEL_TYPES, "groups": ItemGroup.objects.order_by("name"), "docs": docs,
        "includes_tax": prices_include_tax_default(), "tax_rate": request.company.default_tax_rate,
        "missing_barcodes": Item.objects.filter(
            disabled=False, barcode="").count(),
    })


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _print_labels(request):
    company = request.company
    try:
        rows = json.loads(request.POST.get("rows") or "[]")
    except json.JSONDecodeError:
        rows = []
    size_key = request.POST.get("size") if request.POST.get("size") in LABEL_SIZES else "50x25"
    kind = request.POST.get("type") if request.POST.get("type") in LABEL_TYPES else "item"
    includes_tax = bool(request.POST.get("includes_tax"))
    show = {k: bool(request.POST.get(k)) for k in ("show_company", "show_code", "show_expiry", "show_vat_note")}
    items = Item.objects.in_bulk([r.get("id") for r in rows if str(r.get("id", "")).isdigit()])
    labels_out, svg_cache = [], {}
    for r in rows:
        item = items.get(int(r["id"])) if str(r.get("id", "")).isdigit() else None
        if item is None:
            continue
        try:
            copies = max(0, min(int(r.get("copies") or 1), MAX_LABELS))
        except (TypeError, ValueError):
            copies = 1
        try:
            price = flt(Decimal(str(r.get("price")))) if r.get("price") not in (None, "") else None
        except InvalidOperation:
            price = None
        if price is None:
            price = shelf_price(item, item.selling_rate, includes_tax, company)
        code = item.barcode or item.code
        if code not in svg_cache:
            try:
                svg_cache[code] = barcode.svg(code)
            except barcode.BarcodeError:
                svg_cache[code] = ""
        label = {"item": item, "price": price, "code": code, "svg": svg_cache[code],
                 "batch": str(r.get("batch") or "")[:40], "expiry": _parse_date(r.get("expiry")),
                 "production": _parse_date(r.get("production"))}
        labels_out.extend([label] * copies)
        if len(labels_out) >= MAX_LABELS:
            labels_out = labels_out[:MAX_LABELS]
            break
    size = LABEL_SIZES[size_key]
    pages = []
    if "cols" in size:
        try:
            skip = max(0, min(int(request.POST.get("start_at") or 1) - 1, size["cols"] * size["rows"] - 1))
        except ValueError:
            skip = 0
        cells = [None] * skip + labels_out
        per_page = size["cols"] * size["rows"]
        pages = [cells[i:i + per_page] for i in range(0, len(cells), per_page)]
    from core.models import PrintSettings

    # القيم بالمليمتر كنصوص بالنقطة العشرية (لا تتأثر بتنسيق الأرقام المحلي)
    mm = {k: f"{v}mm" for k, v in size.items() if isinstance(v, (int, float))}
    return render(request, "stock/labels_print.html", {
        "mm": mm, "labels": labels_out, "pages": pages, "size": size, "size_key": size_key, "kind": kind, "show": show,
        "company": company, "ps": PrintSettings.for_company(company), "today": timezone.localdate(),
        "includes_tax": includes_tax, "currency": company.currency,
    })


@login_required
@require_POST
def generate_barcodes(request):
    """باركود داخلي EAN-13 (يبدأ بـ 200) للأصناف التي ليس لها باركود."""
    membership = getattr(request, "membership", None)
    if membership is None or not membership.can_manage:
        messages.error(request, "توليد الباركود متاح لمدير الشركة فقط.")
        return redirect("stock_labels")
    used = set(Item.objects.exclude(barcode="").values_list("barcode", flat=True))
    count = 0
    with transaction.atomic():
        for item in Item.objects.filter(disabled=False, barcode="").order_by("id"):
            seq = item.id
            code = barcode.internal_ean13(seq)
            while code in used:
                seq += 10 ** 6
                code = barcode.internal_ean13(seq)
            item.barcode = code
            item.save(update_fields=["barcode"])
            used.add(code)
            count += 1
    messages.success(request, f"تم توليد باركود داخلي لـ {count} صنف.")
    return redirect("stock_labels")
