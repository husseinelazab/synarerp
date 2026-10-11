"""تحويل طلبات سلة وزد إلى صيغة موحدة، والتحقق من مصدر الـ Webhook.

صيغ الحمولات مبنية على الأمثلة المنشورة للمنصتين، والقراءة مرنة: كل حقل يُبحث عنه بأكثر من اسم محتمل،
والطلب الذي لا تكتمل بياناته يذهب لقائمة المراجعة مع سببه بدل أن يُسجل خطأً، وتُحفظ الحمولة الأصلية كما وصلت.
"""
import hashlib
import hmac
from decimal import Decimal, InvalidOperation

CANCELLED_STATUSES = {"canceled", "cancelled", "refunded", "returned", "restored", "deleted", "ملغي", "مسترجع"}
UNPAID_METHODS = {"cod", "zid_cod", "cash_on_delivery", "bank", "bank_transfer", "waiting"}


def dig(data, *paths, default=None):
    """أول قيمة غير فارغة من عدة مسارات منقطة: dig(d, "customer.mobile", "customer.phone")."""
    for path in paths:
        node = data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                node = None
                break
        if node not in (None, "", [], {}):
            return node
    return default


def amount(value):
    """مبلغ من رقم أو نص أو كائن {amount: ..} كما تعيده المنصتان."""
    if isinstance(value, dict):
        value = value.get("amount", value.get("value"))
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def first_amount(data, *paths):
    for path in paths:
        value = amount(dig(data, path))
        if value is not None:
            return value
    return None


def _status(value):
    if isinstance(value, dict):
        value = value.get("slug") or value.get("code") or value.get("name")
    return str(value or "").strip().lower()


def _payment(value):
    if isinstance(value, dict):
        value = value.get("code") or value.get("slug") or value.get("name")
    return str(value or "").strip().lower()


# ---------------- سلة ----------------

def normalize_salla(body):
    """حمولة سلة: {event, merchant, created_at, data: {الطلب}}."""
    data = body.get("data") or {}
    customer = data.get("customer") or {}
    first, last = customer.get("first_name") or "", customer.get("last_name") or ""
    mobile = str(customer.get("mobile") or "")
    code = str(customer.get("mobile_code") or "").lstrip("+")
    if mobile and code and not mobile.startswith(("0", "+", code)):
        mobile = code + mobile
    items = []
    for row in data.get("items") or []:
        qty = amount(row.get("quantity")) or Decimal(0)
        unit_net = first_amount(row, "amounts.price_without_tax", "price_without_tax")
        unit_gross = first_amount(row, "amounts.price_with_tax", "price_with_tax", "price")
        line_total = first_amount(row, "amounts.total", "total")
        items.append({"sku": str(dig(row, "sku", "product.sku", "product_sku", default="")).strip(),
                      "name": dig(row, "name", "product.name", default=""), "qty": qty,
                      "unit_net": unit_net, "unit_gross": unit_gross, "line_total": line_total})
    return {
        "event": body.get("event", ""), "store_id": str(body.get("merchant") or ""),
        "external_id": str(dig(data, "id", default="")), "number": str(dig(data, "reference_id", "id", default="")),
        "date": dig(data, "date.date", "created_at.date", "date", "created_at"),
        "status": _status(dig(data, "status")),
        "payment_method": _payment(dig(data, "payment_method")),
        "currency": dig(data, "currency", "amounts.total.currency", default="SAR"),
        "customer": {"name": f"{first} {last}".strip() or dig(customer, "name", default=""), "phone": mobile,
                     "email": customer.get("email") or "", "city": dig(customer, "city", default="")},
        "items": items,
        "shipping": first_amount(data, "amounts.shipping_cost", "shipping_cost") or Decimal(0),
        "discount": _salla_discount(data),
        "total": first_amount(data, "amounts.total", "total"),
        "tax": first_amount(data, "amounts.tax.amount", "amounts.tax"),
    }


def _salla_discount(data):
    discounts = dig(data, "amounts.discounts", default=[])
    if isinstance(discounts, list):
        return sum((amount(d.get("discount") if isinstance(d, dict) else d) or Decimal(0) for d in discounts), Decimal(0))
    return amount(discounts) or Decimal(0)


# ---------------- زد ----------------

def normalize_zid(body):
    """حمولة زد: كائن الطلب مباشرة (أو داخل order/data)، والحدث من الحقل event أو من الرابط."""
    data = body.get("order") or body.get("data") or body
    customer = data.get("customer") or {}
    items = []
    for row in data.get("products") or data.get("items") or []:
        qty = amount(row.get("quantity")) or Decimal(0)
        items.append({"sku": str(dig(row, "sku", "product.sku", default="")).strip(),
                      "name": dig(row, "name", "product.name", default=""), "qty": qty,
                      "unit_net": first_amount(row, "price_without_tax", "net_price"),
                      "unit_gross": first_amount(row, "price_with_tax", "price"),
                      "line_total": first_amount(row, "total_with_tax", "total")})
    return {
        "event": body.get("event", ""), "store_id": str(dig(data, "store_id", "store.id", default="")),
        "external_id": str(dig(data, "id", default="")), "number": str(dig(data, "code", "id", default="")),
        "date": dig(data, "created_at", "date"),
        "status": _status(dig(data, "order_status", "status")),
        "payment_method": _payment(dig(data, "payment.method", "payment_method")),
        "currency": dig(data, "currency_code", "currency.code", "currency", default="SAR"),
        "customer": {"name": dig(customer, "name", default=""), "phone": str(dig(customer, "mobile", "phone", default="")),
                     "email": dig(customer, "email", default=""), "city": dig(customer, "city.name", "city", default="")},
        "items": items,
        "shipping": first_amount(data, "shipping.method.shipping_fee", "shipping_fee", "shipping.fee") or Decimal(0),
        "discount": first_amount(data, "discount_amount", "coupon.discount", "discount") or Decimal(0),
        "total": first_amount(data, "order_total", "total"),
        "tax": first_amount(data, "taxes_amount", "vat"),
    }


NORMALIZERS = {"salla": normalize_salla, "zid": normalize_zid}


def verify(connection, request, raw):
    """المفتاح السري في الرابط يكفي لزد؛ وإن ضُبط «السر» يُشترط أيضاً: توقيع سلة HMAC-SHA256 أو ترويسة Authorization."""
    secret = connection.webhook_secret
    if not secret:
        return True
    signature = request.headers.get("X-Salla-Signature") or request.headers.get("X-Zid-Signature") or ""
    if signature:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.strip().lower(), expected)
    token = (request.headers.get("Authorization") or request.GET.get("token") or "").removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, secret)
