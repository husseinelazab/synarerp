"""التشفير الخاص بالفوترة الإلكترونية (هيئة الزكاة والضريبة والجمارك - المرحلة الثانية).

- مفتاح ECDSA على منحنى secp256k1 لكل وحدة إصدار فواتير (EGS).
- طلب توقيع شهادة (CSR) بالامتدادات التي تشترطها الهيئة.
- تخزين المفتاح الخاص مشفراً (Fernet) بمفتاح مشتق من SECRET_KEY.
- التوقيع الرقمي لهاش الفاتورة واستخراج بيانات الشهادة بالصيغ التي تطلبها الهيئة.
"""
import base64
import hashlib
from dataclasses import dataclass

from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier
from django.conf import settings

TEMPLATE_NAME_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2")  # certificateTemplateName
REGISTERED_ADDRESS_OID = ObjectIdentifier("2.5.4.26")

# اسم قالب الشهادة حسب البيئة
TEMPLATE_NAMES = {
    "sandbox": "TSTZATCA-Code-Signing",
    "simulation": "PREZATCA-Code-Signing",
    "production": "ZATCA-Code-Signing",
}


# ---------------- تخزين الأسرار ----------------

def _fernet():
    key = hashlib.sha256((settings.SECRET_KEY + ":synar-zatca").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("تعذر فك تشفير مفتاح الفوترة الإلكترونية؛ هل تغيّر SECRET_KEY؟")


# ---------------- المفاتيح وطلب الشهادة ----------------

def generate_private_key_pem():
    key = ec.generate_private_key(ec.SECP256K1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


def load_private_key(pem):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _der_utf8_string(value):
    data = value.encode("utf-8")
    if len(data) > 127:
        raise ValueError("اسم القالب طويل جداً")
    return bytes([0x0C, len(data)]) + data


@dataclass
class CsrInfo:
    common_name: str          # معرّف الوحدة (نص حر)
    serial_number: str        # 1-اسم الحل|2-الإصدار|3-الرقم التسلسلي
    vat_number: str           # الرقم الضريبي (15 رقماً)
    organization_unit: str    # اسم الفرع
    organization_name: str    # اسم المكلف
    invoice_types: str        # مثل 1100: ضريبية ومبسطة
    location: str             # عنوان الفرع
    industry: str             # النشاط
    environment: str = "sandbox"


def generate_csr_pem(private_key_pem, info: CsrInfo):
    key = load_private_key(private_key_pem)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "SA"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, info.organization_unit),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, info.organization_name),
        x509.NameAttribute(NameOID.COMMON_NAME, info.common_name),
    ])
    alt_name = x509.DirectoryName(x509.Name([
        x509.NameAttribute(NameOID.SURNAME, info.serial_number),
        x509.NameAttribute(NameOID.USER_ID, info.vat_number),
        x509.NameAttribute(NameOID.TITLE, info.invoice_types),
        x509.NameAttribute(REGISTERED_ADDRESS_OID, info.location),
        x509.NameAttribute(NameOID.BUSINESS_CATEGORY, info.industry),
    ]))
    template = x509.UnrecognizedExtension(TEMPLATE_NAME_OID, _der_utf8_string(TEMPLATE_NAMES[info.environment]))
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .add_extension(template, critical=False)
        .add_extension(x509.SubjectAlternativeName([alt_name]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


# ---------------- الشهادة ----------------

def certificate_body_from_token(binary_security_token):
    """الهيئة تعيد الشهادة مرمزة Base64 مرتين؛ نعيد نص الشهادة (Base64 لـ DER) بدون ترويسة PEM."""
    return base64.b64decode(binary_security_token).decode().strip()


def certificate_pem(body):
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


@dataclass
class CertificateInfo:
    body: str             # Base64 لـ DER كما يُضمّن في X509Certificate
    digest: str           # base64(hex(sha256(body))) كما تشترط الهيئة
    issuer: str           # بالصيغة المعكوسة «CN=..., DC=...»
    serial_number: str    # رقم عشري
    public_key_der: bytes
    signature: bytes


def certificate_info(body):
    certificate = x509.load_pem_x509_certificate(certificate_pem(body).encode())
    digest_hex = hashlib.sha256(body.encode()).hexdigest()
    issuer = ", ".join(rdn.rfc4514_string() for rdn in reversed(certificate.issuer.rdns))
    return CertificateInfo(
        body=body,
        digest=base64.b64encode(digest_hex.encode()).decode(),
        issuer=issuer,
        serial_number=str(certificate.serial_number),
        public_key_der=certificate.public_key().public_bytes(serialization.Encoding.DER,
                                                             serialization.PublicFormat.SubjectPublicKeyInfo),
        signature=certificate.signature,
    )


def sign_invoice_hash(private_key_pem, invoice_hash_b64):
    """التوقيع الرقمي (KSA-15): ECDSA-SHA256 على بايتات هاش الفاتورة، بترميز DER ثم Base64.

    ملاحظة: معيار «ميزات الأمان» للهيئة ومجموعة تطوير البرمجيات الرسمية يوقّعان هاش الفاتورة.
    إن أظهرت بيئة المحاكاة خلاف ذلك فهذا هو الموضع الوحيد الذي يلزم تعديله.
    """
    key = load_private_key(private_key_pem)
    signature = key.sign(base64.b64decode(invoice_hash_b64), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


def verify_invoice_signature(public_key_der, invoice_hash_b64, signature_b64):
    public_key = serialization.load_der_public_key(public_key_der)
    public_key.verify(base64.b64decode(signature_b64), base64.b64decode(invoice_hash_b64), ec.ECDSA(hashes.SHA256()))
    return True
