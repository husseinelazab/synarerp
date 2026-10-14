"""عميل بوابة ميسّر (Moyasar) المبسّط. الاختبارات تستبدل fetch_payment و create_payment بدوال وهمية."""
import base64
import json
import logging
import urllib.error
import urllib.request

API_BASE = "https://api.moyasar.com/v1"
JS_URL = "https://cdn.moyasar.com/mpf/1.14.0/moyasar.js"
CSS_URL = "https://cdn.moyasar.com/mpf/1.14.0/moyasar.css"
TIMEOUT = 20

log = logging.getLogger(__name__)


class MoyasarError(Exception):
    pass


def _request(method, path, secret_key, payload=None):
    if not secret_key:
        raise MoyasarError("المفتاح السري لبوابة ميسّر غير مُعد.")
    auth = base64.b64encode(f"{secret_key}:".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method, headers={
        "Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
        except ValueError:
            body = {}
        if method == "POST" and isinstance(body, dict) and body.get("id"):
            return body  # عملية مرفوضة لها سجل (status=failed)
        raise MoyasarError(body.get("message") if isinstance(body, dict) and body.get("message")
                           else f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise MoyasarError(str(exc)) from exc


def fetch_payment(secret_key, payment_id):
    """GET /payments/<id>: المصدر الوحيد الموثوق لحالة الدفع."""
    if not payment_id or not all(c.isalnum() or c == "-" for c in payment_id):
        raise MoyasarError("رقم عملية غير صالح.")
    return _request("GET", f"/payments/{payment_id}", secret_key)


def create_payment(secret_key, payload):
    """POST /payments (للخصم الشهري بالبطاقة المحفوظة source.type=token)."""
    return _request("POST", "/payments", secret_key, payload)
