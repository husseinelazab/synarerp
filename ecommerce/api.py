"""الاتصال بواجهات سلة وزد: التحقق من الوصول، وقراءة المنتجات، وإرسال كميات المخزون.

العناوين والترويسات من التوثيق المنشور للمنصتين:
- سلة: https://api.salla.dev/admin/v2 بترويسة Authorization: Bearer، وتحديث الكميات بالجملة POST /products/quantities/bulk
  (بمعرّف SKU ووضع overwrite)، وتجديد الرمز من https://accounts.salla.sa/oauth2/token.
- زد: https://api.zid.sa/v1 بترويسات Authorization (رمز الشريك) وAccess-Token وStore-Id وRole: Manager،
  والتحديث بالجملة PATCH /products/ (حتى 50 منتجاً في الطلب).
كل الاستدعاءات هنا فقط، فأي تغيير في المنصة يُعدّل في مكان واحد، وزر «اختبار الاتصال» يعرض رد المنصة كما هو.
"""
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from django.utils import timezone

from ecommerce.platforms import amount, dig

_urlopen = urllib.request.urlopen  # يُستبدل في الاختبارات
TIMEOUT = 30
BATCH = 50

SALLA_API = "https://api.salla.dev/admin/v2"
SALLA_TOKEN_URL = "https://accounts.salla.sa/oauth2/token"
SALLA_USER_INFO = "https://accounts.salla.sa/oauth2/user/info"
ZID_API = "https://api.zid.sa/v1"


class ApiError(Exception):
    pass


def _request(method, url, headers=None, body=None, form=False):
    data = None
    headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _urlopen(request, timeout=TIMEOUT) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise ApiError(f"ردت المنصة بالرمز {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiError(f"تعذر الاتصال بالمنصة: {exc}") from exc
    try:
        return json.loads(text) if text else {}
    except ValueError as exc:
        raise ApiError(f"رد غير مفهوم من المنصة: {text[:200]}") from exc


def _name(value):
    if isinstance(value, dict):
        return value.get("ar") or value.get("en") or next(iter(value.values()), "")
    return str(value or "")


def _qty(value):
    value = amount(value)
    return None if value is None else Decimal(value)


class SallaClient:
    def __init__(self, connection):
        self.connection = connection

    def _headers(self):
        self._ensure_token()
        token = self.connection.access_token
        if not token:
            raise ApiError("لا يوجد رمز وصول لسلة؛ ثبّت التطبيق في المتجر أو أدخل الرمز.")
        return {"Authorization": f"Bearer {token}"}

    def _ensure_token(self):
        """تجديد الرمز قبل انتهائه بيوم إن توفر رمز التجديد وبيانات التطبيق."""
        conn = self.connection
        if not (conn.token_expires_at and conn.refresh_token_enc and conn.client_id and conn.client_secret_enc):
            return
        if conn.token_expires_at - timezone.now() > datetime.timedelta(days=1):
            return
        result = _request("POST", SALLA_TOKEN_URL, body={
            "grant_type": "refresh_token", "refresh_token": conn._secret("refresh_token_enc"),
            "client_id": conn.client_id, "client_secret": conn._secret("client_secret_enc")}, form=True)
        if not result.get("access_token"):
            raise ApiError("تعذر تجديد رمز الوصول لسلة.")
        conn.set_tokens(result["access_token"], result.get("refresh_token"), result.get("expires_in"))
        conn.save(update_fields=["access_token_enc", "refresh_token_enc", "token_expires_at"])

    def test(self):
        info = _request("GET", SALLA_USER_INFO, self._headers())
        return f"متصل بمتجر «{dig(info, 'data.merchant.name', 'data.name', default='؟')}» " \
               f"(رقم {dig(info, 'data.merchant.id', default='؟')})"

    def products(self):
        page = 1
        while True:
            result = _request("GET", f"{SALLA_API}/products?page={page}&per_page={BATCH}", self._headers())
            for row in result.get("data") or []:
                unlimited = bool(row.get("unlimited_quantity"))
                yield {"external_id": str(row.get("id")), "sku": str(row.get("sku") or "").strip(),
                       "name": _name(row.get("name")), "price": amount(row.get("price")),
                       "quantity": None if unlimited else _qty(row.get("quantity"))}
            total_pages = dig(result, "pagination.totalPages", "pagination.total_pages", default=page)
            if page >= int(total_pages or page):
                return
            page += 1

    def push_quantities(self, rows):
        """rows: [(منتج المتجر، الكمية)] بالـ SKU."""
        for start in range(0, len(rows), BATCH):
            chunk = rows[start:start + BATCH]
            _request("POST", f"{SALLA_API}/products/quantities/bulk", self._headers(), {"products": [
                {"identifer_type": "sku", "identifer": product.sku, "quantity": int(qty), "mode": "overwrite"}
                for product, qty in chunk]})


class ZidClient:
    def __init__(self, connection):
        self.connection = connection

    def _headers(self):
        conn = self.connection
        if not conn.access_token or not conn.partner_token_enc or not conn.store_id:
            raise ApiError("أدخل رمز الشريك (Authorization) ورمز المتجر (Access-Token) ورقم المتجر لزد.")
        return {"Authorization": f"Bearer {conn._secret('partner_token_enc')}", "Access-Token": conn.access_token,
                "Store-Id": conn.store_id, "Role": "Manager", "Accept-Language": "ar"}

    def test(self):
        result = _request("GET", f"{ZID_API}/products/?page=1&page_size=1", self._headers())
        return f"متصل بمتجر زد رقم {self.connection.store_id} — عدد المنتجات {result.get('count', '؟')}"

    def products(self):
        page = 1
        while True:
            result = _request("GET", f"{ZID_API}/products/?page={page}&page_size={BATCH}", self._headers())
            for row in result.get("results") or []:
                infinite = bool(row.get("is_infinite"))
                yield {"external_id": str(row.get("id")), "sku": str(row.get("sku") or "").strip(),
                       "name": _name(row.get("name")), "price": amount(row.get("price")),
                       "quantity": None if infinite else _qty(row.get("quantity"))}
            if not result.get("next"):
                return
            page += 1

    def push_quantities(self, rows):
        for start in range(0, len(rows), BATCH):
            chunk = rows[start:start + BATCH]
            _request("PATCH", f"{ZID_API}/products/", self._headers(), [
                {"id": product.external_id, "sku": product.sku, "quantity": int(qty), "is_infinite": False}
                for product, qty in chunk])


CLIENTS = {"salla": SallaClient, "zid": ZidClient}


def client(connection):
    return CLIENTS[connection.platform](connection)
