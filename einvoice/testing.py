"""أدوات للاختبارات: شهادة تحاكي بنية شهادات الهيئة (مُصدرة من جهة وسيطة) دون الاتصال بالهيئة."""
import base64
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from einvoice.crypto import load_private_key


def fake_zatca_token(csr_pem):
    """يُصدر شهادة للمفتاح الموجود في CSR من جهة إصدار وهمية، ويعيدها بصيغة binarySecurityToken."""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    issuer_key = ec.generate_private_key(ec.SECP256K1())
    issuer = x509.Name([
        x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "local"),
        x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "gov"),
        x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "extgazt"),
        x509.NameAttribute(NameOID.COMMON_NAME, "TSZEINVOICE-SubCA-1"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(csr.subject).issuer_name(issuer).public_key(csr.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365)).sign(issuer_key, hashes.SHA256())
    )
    body = base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()
    return base64.b64encode(body.encode()).decode()


def public_key_matches(private_key_pem, public_key_der):
    expected = load_private_key(private_key_pem).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return expected == public_key_der


class FakeZatca:
    """محاكاة لمنصة فاتورة للاختبارات: تتحقق فعلياً من هاش كل فاتورة وتوقيعها قبل قبولها."""

    def __init__(self, reporting_status=200, fail_network=False, reject=False):
        self.calls = []
        self.csr = None
        self.certificate_body = None
        self.reporting_status, self.fail_network, self.reject = reporting_status, fail_network, reject

    def _response(self, status, payload):
        import io
        import json
        import urllib.error

        body = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError("https://zatca", status, "error", {}, io.BytesIO(body))

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        response = Response(body)
        response.status = status
        return response

    def _check_invoice(self, body):
        from einvoice.crypto import certificate_info, verify_invoice_signature
        from einvoice.xml import decode_qr, extract_qr, invoice_hash

        xml = base64.b64decode(body["invoice"]).decode()
        errors = []
        if invoice_hash(xml) != body["invoiceHash"]:
            errors.append({"code": "invalid-invoice-hash", "message": "hash mismatch"})
        info = certificate_info(self.certificate_body)
        qr = decode_qr(extract_qr(xml))
        try:
            verify_invoice_signature(info.public_key_der, body["invoiceHash"], qr[7].decode())
        except Exception:
            errors.append({"code": "invalid-signature", "message": "signature invalid"})
        return xml, errors

    def __call__(self, request, timeout=None):
        import json
        import urllib.error

        if self.fail_network:
            raise urllib.error.URLError("network unreachable")
        path = request.full_url.split("/e-invoicing/", 1)[1].split("/", 1)[1]
        body = json.loads(request.data.decode())
        self.calls.append((path, dict(request.headers), body))
        if path == "compliance":
            self.csr = base64.b64decode(body["csr"]).decode()
            token = fake_zatca_token(self.csr)
            self.certificate_body = base64.b64decode(token).decode()
            return self._response(200, {"requestID": 1234567, "binarySecurityToken": token, "secret": "c-secret",
                                        "dispositionMessage": "ISSUED"})
        if path == "production/csids":
            token = fake_zatca_token(self.csr)
            self.certificate_body = base64.b64decode(token).decode()
            return self._response(200, {"requestID": 7654321, "binarySecurityToken": token, "secret": "p-secret"})
        xml, errors = self._check_invoice(body)
        if self.reject or errors:
            return self._response(400, {"validationResults": {"status": "ERROR", "errorMessages":
                                       errors or [{"code": "BR-KSA-01", "message": "rejected"}]}})
        result = {"validationResults": {"status": "PASS", "warningMessages": [], "errorMessages": []}}
        if path.endswith("clearance/single"):
            result.update({"clearanceStatus": "CLEARED", "clearedInvoice": base64.b64encode(xml.encode()).decode()})
        else:
            result["reportingStatus"] = "REPORTED"
        return self._response(self.reporting_status, result)
