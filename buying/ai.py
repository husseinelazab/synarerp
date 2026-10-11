"""قراءة فواتير الموردين بالذكاء الاصطناعي (Claude من Anthropic) بمفتاح الشركة نفسها.

المسار: ملف (صورة أو PDF) ← Claude يستخرج البيانات بمخطط JSON ثابت ← مطابقة المورد والأصناف مع بيانات الشركة
← مراجعة المستخدم ← فاتورة مشتريات «مسودة». لا يُعتمد شيء آلياً، ولا يُحفظ الملف في النظام.
"""
import base64
import json
import re
from decimal import Decimal, InvalidOperation

from core.utils import flt

MAX_IMAGE_BYTES = 5 * 1024 * 1024    # حد Claude للصورة الواحدة
MAX_PDF_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT = 100                 # ثوانٍ؛ أقل من مهلة Gunicorn (120)
# النماذج التي تدعم إعادة الطلب تلقائياً على نموذج آخر إن اعتذر النموذج (Claude API)؛ Haiku لا يدعمها
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

SIGNATURES = [
    (b"%PDF", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

SYSTEM_PROMPT = """You read supplier (purchase) invoices for a Saudi/Arab business ERP and return their data as JSON.
The invoice may be in Arabic, English, or both; it may be a photo, a scan, or a PDF.

Rules:
- Copy names, codes and numbers exactly as printed. Never invent data: use "" for text you cannot find and 0 for numbers.
- Convert Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩) to Western digits.
- supplier_* fields describe the SELLER who issued the invoice, not the buyer.
- supplier_tax_id is the seller's VAT registration number (in Saudi Arabia, 15 digits starting and ending with 3).
- invoice_date in YYYY-MM-DD (convert Hijri dates to Gregorian only if no Gregorian date is printed).
- One entry in lines per invoice line, in printed order. unit_price is the price per unit after any per-line discount.
- prices_include_vat is true only if the printed unit prices already include VAT.
- subtotal is the total before VAT, tax_amount the VAT amount, grand_total the total payable.
- invoice_discount is any discount applied to the invoice as a whole (not per line), else 0.
- notes: anything a bookkeeper should check (unclear digits, handwriting, missing pages, totals that do not add up). Write notes in Arabic."""

_LINE = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "item_code": {"type": "string"},
        "barcode": {"type": "string"},
        "unit": {"type": "string"},
        "qty": {"type": "number"},
        "unit_price": {"type": "number"},
        "line_total": {"type": "number"},
    },
    "required": ["description", "item_code", "barcode", "unit", "qty", "unit_price", "line_total"],
    "additionalProperties": False,
}
INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string"},
        "supplier_tax_id": {"type": "string"},
        "bill_no": {"type": "string"},
        "invoice_date": {"type": "string"},
        "currency": {"type": "string"},
        "prices_include_vat": {"type": "boolean"},
        "tax_rate": {"type": "number"},
        "lines": {"type": "array", "items": _LINE},
        "invoice_discount": {"type": "number"},
        "subtotal": {"type": "number"},
        "tax_amount": {"type": "number"},
        "grand_total": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["supplier_name", "supplier_tax_id", "bill_no", "invoice_date", "currency", "prices_include_vat",
                 "tax_rate", "lines", "invoice_discount", "subtotal", "tax_amount", "grand_total", "notes"],
    "additionalProperties": False,
}


class AIError(Exception):
    """خطأ يُعرض للمستخدم كما هو."""


def detect_mime(data):
    for signature, mime in SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_file(data):
    """نوع الملف من محتواه لا من امتداده، وحجمه ضمن حدود Claude."""
    mime = detect_mime(data)
    if mime is None:
        raise AIError("صيغة غير مدعومة. ارفع صورة (JPG أو PNG أو WebP) أو ملف PDF.")
    limit = MAX_PDF_BYTES if mime == "application/pdf" else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise AIError(f"حجم الملف أكبر من {limit // (1024 * 1024)} ميجابايت. صغّر الصورة أو قسّم الملف.")
    return mime


def _client(api_key):
    import anthropic

    return anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=0)


def _api_error(exc):
    """رسالة عربية مفهومة لأخطاء Anthropic (الأخص أولاً)."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "مفتاح API غير صحيح أو ملغى. حدّثه من «الإعدادات ← الذكاء الاصطناعي»."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "المفتاح لا يملك صلاحية استخدام هذا النموذج. تحقق من حسابك في console.anthropic.com."
    if isinstance(exc, anthropic.NotFoundError):
        return "النموذج المختار غير متاح لحسابك. اختر نموذجاً آخر من إعدادات الذكاء الاصطناعي."
    if isinstance(exc, anthropic.RateLimitError):
        return "تجاوزت حد الاستخدام المسموح لحسابك لدى Anthropic. حاول بعد دقيقة."
    if isinstance(exc, anthropic.BadRequestError):
        # مثل نفاد الرصيد: الرسالة من Anthropic بالإنجليزية وتوضح السبب
        return f"رفضت Anthropic الطلب: {exc.message}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "استغرقت قراءة الفاتورة وقتاً أطول من المسموح. جرّب صورة أوضح أو نموذجاً أسرع."
    if isinstance(exc, anthropic.APIStatusError):
        return "خدمة Anthropic غير متاحة مؤقتاً. حاول بعد قليل."
    if isinstance(exc, anthropic.APIConnectionError):
        return "تعذر الاتصال بخدمة Anthropic. تحقق من اتصال الخادم بالإنترنت."
    return "حدث خطأ غير متوقع أثناء قراءة الفاتورة."


def verify_key(api_key, model):
    """التحقق من المفتاح والنموذج دون استهلاك رصيد (قراءة بيانات النموذج فقط)."""
    import anthropic

    try:
        _client(api_key).models.retrieve(model)
    except anthropic.AnthropicError as exc:
        raise AIError(_api_error(exc)) from exc


def extract_invoice(api_key, model, data, mime):
    """يرسل الملف إلى Claude ويعيد بيانات الفاتورة وفق INVOICE_SCHEMA."""
    import anthropic

    encoded = base64.standard_b64encode(data).decode()
    block_type = "document" if mime == "application/pdf" else "image"
    request = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": mime, "data": encoded}},
            {"type": "text", "text": "Extract this supplier invoice."},
        ]}],
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": INVOICE_SCHEMA}},
    }
    if model in FALLBACK_MODELS:
        request.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        response = _client(api_key).beta.messages.create(**request)
    except anthropic.AnthropicError as exc:
        raise AIError(_api_error(exc)) from exc
    if response.stop_reason == "refusal":
        raise AIError("اعتذر النموذج عن قراءة هذا الملف. تأكد أنه فاتورة مورد، أو أدخلها يدوياً.")
    if response.stop_reason == "max_tokens":
        raise AIError("الفاتورة طويلة جداً لقراءتها مرة واحدة. قسّمها إلى أجزاء وارفع كل جزء.")
    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        result = json.loads(text)
    except ValueError as exc:
        raise AIError("تعذرت قراءة رد النموذج. حاول مرة أخرى.") from exc
    if not result.get("lines"):
        raise AIError("لم يُعثر على بنود في الملف. تأكد أن الصورة واضحة وتظهر الفاتورة كاملة.")
    return result


# ---------------- المطابقة مع بيانات الشركة ----------------

def _digits(text):
    return re.sub(r"\D", "", text or "")


def match_supplier(name, tax_id):
    """المورد بالرقم الضريبي أولاً، ثم بالاسم المطابق، ثم بجزء من الاسم إن كان مورداً واحداً."""
    from buying.models import Supplier

    suppliers = Supplier.objects.filter(disabled=False)
    tax = _digits(tax_id)
    if len(tax) >= 5:
        for supplier in suppliers.exclude(tax_id="").only("id", "name", "tax_id"):
            if _digits(supplier.tax_id) == tax:
                return supplier
    name = (name or "").strip()
    if name:
        exact = suppliers.filter(name__iexact=name).first()
        if exact:
            return exact
        partial = list(suppliers.filter(name__icontains=name)[:2])
        if len(partial) == 1:
            return partial[0]
    return None


def match_item(line):
    """(الصنف، الوحدة، طريقة المطابقة) من الباركود ثم الكود ثم الاسم."""
    from stock.models import Item, ItemUOM

    items = Item.objects.filter(disabled=False)
    for code in (line.get("barcode", ""), line.get("item_code", "")):
        code = (code or "").strip()
        if not code:
            continue
        item = items.filter(barcode=code).first()
        if item:
            return item, None, "الباركود"
        unit = ItemUOM.objects.filter(barcode=code, item__disabled=False).select_related("item").first()
        if unit:
            return unit.item, unit.uom_id, "باركود الوحدة"
        item = items.filter(code__iexact=code).first()
        if item:
            return item, None, "كود الصنف"
    name = (line.get("description") or "").strip()
    if name:
        item = items.filter(name__iexact=name).first()
        if item:
            return item, None, "الاسم"
        partial = list(items.filter(name__icontains=name)[:2])
        if len(partial) == 1:
            return partial[0], None, "جزء من الاسم"
    return None, None, ""


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def to_decimal(value, places=2):
    """رقم من رد النموذج أو من إدخال المستخدم (يقبل الأرقام الهندية والفاصلة العشرية العربية)."""
    if isinstance(value, str):
        value = value.strip().translate(ARABIC_DIGITS).replace(" ", "")
        # «5,50» فاصلة عشرية؛ «1,250.00» فاصل آلاف
        value = value.replace(",", ".") if "," in value and "." not in value else value.replace(",", "")
    try:
        return flt(Decimal(str(value or 0)), places)
    except (InvalidOperation, ValueError):
        return flt(0, places)


def build_review(result):
    """يجهّز بيانات صفحة المراجعة: المورد المطابق، وأسطر بأسعار غير شاملة للضريبة، وفحص المجاميع."""
    tax_rate = to_decimal(result.get("tax_rate"))
    include_vat = bool(result.get("prices_include_vat")) and tax_rate > 0
    rows, net = [], flt(0)
    for line in result.get("lines", []):
        item, uom_id, matched_by = match_item(line)
        qty = to_decimal(line.get("qty"), 3)
        rate = to_decimal(line.get("unit_price"))
        if include_vat:
            # فاتورة المشتريات في النظام بأسعار غير شاملة للضريبة
            rate = flt(rate * 100 / (100 + tax_rate))
        rows.append({"description": line.get("description", ""), "item_code": line.get("item_code", ""),
                     "unit": line.get("unit", ""), "qty": qty, "rate": rate,
                     "item_id": item.pk if item else None, "uom_id": uom_id, "matched_by": matched_by})
        net += flt(qty * rate)
    expected = to_decimal(result.get("subtotal"))
    warnings = []
    if expected and abs(net - expected) > 1:
        warnings.append(f"مجموع البنود قبل الضريبة ({net}) يختلف عن الإجمالي المطبوع ({expected}). راجع الكميات والأسعار.")
    if to_decimal(result.get("invoice_discount")):
        warnings.append("على الفاتورة خصم إجمالي، وفاتورة المشتريات لا تدعمه؛ وزّعه على أسعار البنود قبل الاعتماد.")
    if result.get("notes"):
        warnings.append(result["notes"])
    supplier = match_supplier(result.get("supplier_name"), result.get("supplier_tax_id"))
    return {"supplier": supplier, "rows": rows, "net_total": net, "tax_rate": tax_rate, "warnings": warnings}
