"""عميل واجهات منصة فاتورة (هيئة الزكاة والضريبة والجمارك) - الإصدار V2."""
import base64
import json
import urllib.error
import urllib.request

BASE_URLS = {
    "sandbox": "https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal",
    "simulation": "https://gw-fatoora.zatca.gov.sa/e-invoicing/simulation",
    "production": "https://gw-fatoora.zatca.gov.sa/e-invoicing/core",
}
TIMEOUT = 30

_urlopen = urllib.request.urlopen  # يُستبدل في الاختبارات


class ZatcaError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"ZATCA {status}")
        self.status = status
        self.payload = payload

    @property
    def messages(self):
        return error_messages(self.payload) or [f"خطأ من منصة فاتورة (رمز {self.status})."]


def error_messages(payload):
    """استخراج رسائل الأخطاء والتحذيرات من رد الهيئة بأي من صيغه."""
    if not isinstance(payload, dict):
        return [str(payload)] if payload else []
    messages = []
    results = payload.get("validationResults") or {}
    for key in ("errorMessages", "warningMessages"):
        for item in results.get(key) or []:
            messages.append(f"{item.get('code', '')}: {item.get('message', '')}".strip(": "))
    for item in payload.get("errors") or []:
        messages.append(item.get("message", str(item)) if isinstance(item, dict) else str(item))
    if payload.get("message") and not messages:
        messages.append(str(payload["message"]))
    return messages


class ZatcaClient:
    def __init__(self, environment, token=None, secret=None):
        self.base_url = BASE_URLS[environment]
        self.token, self.secret = token, secret

    def _post(self, path, body, headers=None):
        request_headers = {"Accept": "application/json", "Content-Type": "application/json",
                           "Accept-Version": "V2", "Accept-Language": "ar"}
        if self.token:
            credentials = base64.b64encode(f"{self.token}:{self.secret}".encode()).decode()
            request_headers["Authorization"] = f"Basic {credentials}"
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(),
                                         headers=request_headers, method="POST")
        try:
            with _urlopen(request, timeout=TIMEOUT) as response:
                return response.status, _json(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ZatcaError(0, {"message": f"تعذر الاتصال بمنصة فاتورة: {getattr(exc, 'reason', exc)}"})

    @staticmethod
    def _invoice_body(xml, invoice_hash, uuid):
        return {"invoiceHash": invoice_hash, "uuid": str(uuid), "invoice": base64.b64encode(xml.encode()).decode()}

    def _expect(self, status, payload, ok=(200,)):
        if status not in ok:
            raise ZatcaError(status, payload)
        return payload

    # ---- الربط (Onboarding) ----
    def compliance_csid(self, csr_pem, otp):
        status, payload = self._post("/compliance", {"csr": base64.b64encode(csr_pem.encode()).decode()},
                                     {"OTP": str(otp)})
        return self._expect(status, payload)

    def compliance_check(self, xml, invoice_hash, uuid):
        return self._post("/compliance/invoices", self._invoice_body(xml, invoice_hash, uuid))

    def production_csid(self, compliance_request_id):
        status, payload = self._post("/production/csids", {"compliance_request_id": str(compliance_request_id)})
        return self._expect(status, payload)

    # ---- الإرسال ----
    def report(self, xml, invoice_hash, uuid):
        return self._post("/invoices/reporting/single", self._invoice_body(xml, invoice_hash, uuid),
                          {"Clearance-Status": "0"})

    def clear(self, xml, invoice_hash, uuid):
        return self._post("/invoices/clearance/single", self._invoice_body(xml, invoice_hash, uuid),
                          {"Clearance-Status": "1"})


def _json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        return {"message": raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)}
