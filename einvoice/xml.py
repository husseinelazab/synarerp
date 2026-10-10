"""بناء الفاتورة الإلكترونية بصيغة UBL 2.1 وفق مواصفات هيئة الزكاة والضريبة والجمارك، وحساب الهاش والتوقيع.

ترتيب العناصر يتبع مخطط UBL 2.1 حرفياً. الهاش يُحسب بحذف UBLExtensions و cac:Signature ومرجع QR
مع إبقاء المسافات المحيطة كما يفعل تحويل XSLT في مجموعة تطوير الهيئة، ثم Canonical XML ثم SHA-256.
"""
import base64
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from xml.sax.saxutils import escape

from lxml import etree

from einvoice.crypto import CertificateInfo, sign_invoice_hash

NS = {
    None: "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
}
SIG_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2"
SAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:SignatureAggregateComponents-2"
SBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:SignatureBasicComponents-2"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
XADES_NS = "http://uri.etsi.org/01903/v1.3.2#"
SHA256_URI = "http://www.w3.org/2001/04/xmlenc#sha256"
C14N11_URI = "http://www.w3.org/2006/12/xml-c14n11"
QR_PLACEHOLDER = "__SYNAR_QR__"

INVOICE, CREDIT_NOTE, DEBIT_NOTE = "388", "381", "383"
STANDARD_NAME, SIMPLIFIED_NAME = "0100000", "0200000"


@dataclass
class Party:
    name: str = ""
    vat: str = ""
    crn: str = ""
    street: str = ""
    building_number: str = ""
    district: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = "SA"


@dataclass
class Line:
    name: str
    quantity: Decimal
    net_amount: Decimal       # LineExtensionAmount (قبل الضريبة)
    tax_amount: Decimal
    unit_code: str = "PCE"

    @property
    def unit_price(self):
        return (self.net_amount / self.quantity).quantize(Decimal("0.000001")).normalize()


@dataclass
class InvoiceData:
    number: str
    uuid: str
    issue_date: str            # YYYY-MM-DD
    issue_time: str            # HH:MM:SS
    type_code: str             # 388 / 381 / 383
    simplified: bool
    currency: str
    icv: int
    pih: str
    seller: Party
    buyer: Party
    lines: list
    tax_rate: Decimal
    line_extension: Decimal
    allowance: Decimal
    tax_exclusive: Decimal
    tax_amount: Decimal
    tax_inclusive: Decimal
    tax_amount_sar: Decimal
    supply_date: str = ""
    payment_means: str = "10"
    billing_reference: str = ""
    reason: str = ""
    tax_currency: str = "SAR"
    vat_category: str = "S"
    exemption_code: str = ""
    exemption_reason: str = ""
    extra: dict = field(default_factory=dict)


def amount(value):
    return f"{Decimal(value):.2f}"


def _el(parent, tag, text=None, **attrs):
    prefix, name = tag.split(":") if ":" in tag else (None, tag)
    element = etree.SubElement(parent, f"{{{NS[prefix]}}}{name}", attrs)
    if text is not None:
        element.text = str(text)
    return element


def _postal_address(parent, party):
    address = _el(parent, "cac:PostalAddress")
    for tag, value in (("cbc:StreetName", party.street), ("cbc:BuildingNumber", party.building_number),
                       ("cbc:CitySubdivisionName", party.district), ("cbc:CityName", party.city),
                       ("cbc:PostalZone", party.postal_code)):
        if value:
            _el(address, tag, value)
    _el(_el(address, "cac:Country"), "cbc:IdentificationCode", party.country or "SA")


def _tax_category(parent, rate, tag="cac:TaxCategory", code="S", exemption=None):
    """فئة الضريبة (S/Z/E/O)؛ سبب الإعفاء يُذكر في فئة ملخص الضريبة فقط وليس في فئة البند."""
    category = _el(parent, tag)
    _el(category, "cbc:ID", code)
    _el(category, "cbc:Percent", amount(rate if code == "S" else 0))
    if exemption and code != "S":
        _el(category, "cbc:TaxExemptionReasonCode", exemption[0])
        _el(category, "cbc:TaxExemptionReason", exemption[1])
    _el(_el(category, "cac:TaxScheme"), "cbc:ID", "VAT")
    return category


def build_unsigned_xml(data: InvoiceData):
    """XML غير موقّع يحتوي UBLExtensions فارغة وعلامة مؤقتة مكان QR."""
    root = etree.Element(f"{{{NS[None]}}}Invoice", nsmap=NS)
    _el(root, "ext:UBLExtensions")
    _el(root, "cbc:ProfileID", "reporting:1.0")
    _el(root, "cbc:ID", data.number)
    _el(root, "cbc:UUID", data.uuid)
    _el(root, "cbc:IssueDate", data.issue_date)
    _el(root, "cbc:IssueTime", data.issue_time)
    _el(root, "cbc:InvoiceTypeCode", data.type_code, name=SIMPLIFIED_NAME if data.simplified else STANDARD_NAME)
    _el(root, "cbc:DocumentCurrencyCode", data.currency)
    _el(root, "cbc:TaxCurrencyCode", data.tax_currency)
    if data.billing_reference:
        _el(_el(_el(root, "cac:BillingReference"), "cac:InvoiceDocumentReference"), "cbc:ID", data.billing_reference)
    icv = _el(root, "cac:AdditionalDocumentReference")
    _el(icv, "cbc:ID", "ICV")
    _el(icv, "cbc:UUID", str(data.icv))
    pih = _el(root, "cac:AdditionalDocumentReference")
    _el(pih, "cbc:ID", "PIH")
    _el(_el(pih, "cac:Attachment"), "cbc:EmbeddedDocumentBinaryObject", data.pih, mimeCode="text/plain")
    qr = _el(root, "cac:AdditionalDocumentReference")
    _el(qr, "cbc:ID", "QR")
    _el(_el(qr, "cac:Attachment"), "cbc:EmbeddedDocumentBinaryObject", QR_PLACEHOLDER, mimeCode="text/plain")
    signature = _el(root, "cac:Signature")
    _el(signature, "cbc:ID", "urn:oasis:names:specification:ubl:signature:Invoice")
    _el(signature, "cbc:SignatureMethod", "urn:oasis:names:specification:ubl:dsig:enveloped:xades")

    seller = _el(_el(root, "cac:AccountingSupplierParty"), "cac:Party")
    _el(_el(seller, "cac:PartyIdentification"), "cbc:ID", data.seller.crn, schemeID="CRN")
    _postal_address(seller, data.seller)
    seller_tax = _el(seller, "cac:PartyTaxScheme")
    _el(seller_tax, "cbc:CompanyID", data.seller.vat)
    _el(_el(seller_tax, "cac:TaxScheme"), "cbc:ID", "VAT")
    _el(_el(seller, "cac:PartyLegalEntity"), "cbc:RegistrationName", data.seller.name)

    customer = _el(root, "cac:AccountingCustomerParty")
    if data.buyer.name:
        buyer = _el(customer, "cac:Party")
        if data.buyer.street or not data.simplified:
            _postal_address(buyer, data.buyer)
        if data.buyer.vat:
            buyer_tax = _el(buyer, "cac:PartyTaxScheme")
            _el(buyer_tax, "cbc:CompanyID", data.buyer.vat)
            _el(_el(buyer_tax, "cac:TaxScheme"), "cbc:ID", "VAT")
        _el(_el(buyer, "cac:PartyLegalEntity"), "cbc:RegistrationName", data.buyer.name)

    if data.supply_date:
        _el(_el(root, "cac:Delivery"), "cbc:ActualDeliveryDate", data.supply_date)
    means = _el(root, "cac:PaymentMeans")
    _el(means, "cbc:PaymentMeansCode", data.payment_means)
    if data.reason:
        _el(means, "cbc:InstructionNote", data.reason)

    if data.allowance:
        allowance = _el(root, "cac:AllowanceCharge")
        _el(allowance, "cbc:ChargeIndicator", "false")
        _el(allowance, "cbc:AllowanceChargeReason", "خصم")
        _el(allowance, "cbc:Amount", amount(data.allowance), currencyID=data.currency)
        _tax_category(allowance, data.tax_rate, code=data.vat_category)

    # إجمالي الضريبة بعملة الضريبة (ريال)، ثم بعملة المستند مع التفصيل حسب الفئة
    _el(_el(root, "cac:TaxTotal"), "cbc:TaxAmount", amount(data.tax_amount_sar), currencyID=data.tax_currency)
    tax_total = _el(root, "cac:TaxTotal")
    _el(tax_total, "cbc:TaxAmount", amount(data.tax_amount), currencyID=data.currency)
    subtotal = _el(tax_total, "cac:TaxSubtotal")
    _el(subtotal, "cbc:TaxableAmount", amount(data.tax_exclusive), currencyID=data.currency)
    _el(subtotal, "cbc:TaxAmount", amount(data.tax_amount), currencyID=data.currency)
    _tax_category(subtotal, data.tax_rate, code=data.vat_category,
                  exemption=(data.exemption_code, data.exemption_reason) if data.exemption_code else None)

    totals = _el(root, "cac:LegalMonetaryTotal")
    _el(totals, "cbc:LineExtensionAmount", amount(data.line_extension), currencyID=data.currency)
    _el(totals, "cbc:TaxExclusiveAmount", amount(data.tax_exclusive), currencyID=data.currency)
    _el(totals, "cbc:TaxInclusiveAmount", amount(data.tax_inclusive), currencyID=data.currency)
    _el(totals, "cbc:AllowanceTotalAmount", amount(data.allowance), currencyID=data.currency)
    _el(totals, "cbc:PrepaidAmount", amount(0), currencyID=data.currency)
    _el(totals, "cbc:PayableAmount", amount(data.tax_inclusive), currencyID=data.currency)

    for index, line in enumerate(data.lines, start=1):
        row = _el(root, "cac:InvoiceLine")
        _el(row, "cbc:ID", index)
        _el(row, "cbc:InvoicedQuantity", f"{Decimal(line.quantity).normalize():f}", unitCode=line.unit_code)
        _el(row, "cbc:LineExtensionAmount", amount(line.net_amount), currencyID=data.currency)
        line_tax = _el(row, "cac:TaxTotal")
        _el(line_tax, "cbc:TaxAmount", amount(line.tax_amount), currencyID=data.currency)
        _el(line_tax, "cbc:RoundingAmount", amount(line.net_amount + line.tax_amount), currencyID=data.currency)
        item = _el(row, "cac:Item")
        _el(item, "cbc:Name", line.name)
        _tax_category(item, data.tax_rate, tag="cac:ClassifiedTaxCategory", code=data.vat_category)
        _el(_el(row, "cac:Price"), "cbc:PriceAmount", f"{line.unit_price:f}", currencyID=data.currency)

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(root, encoding="unicode", pretty_print=True)


# ---------------- الهاش ----------------

def _remove_keep_whitespace(element):
    """حذف العنصر مع إبقاء النص التالي له (المسافات) كما يفعل تحويل XSLT."""
    parent, tail, previous = element.getparent(), element.tail, element.getprevious()
    if tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(element)


def canonical_for_hash(xml_text):
    root = etree.fromstring(xml_text.encode(), etree.XMLParser(remove_blank_text=False))
    ns = {"ext": NS["ext"], "cac": NS["cac"], "cbc": NS["cbc"]}
    targets = root.xpath("ext:UBLExtensions | cac:Signature | cac:AdditionalDocumentReference[cbc:ID='QR']",
                         namespaces=ns)
    for element in targets:
        _remove_keep_whitespace(element)
    return etree.tostring(root.getroottree(), method="c14n")


def invoice_hash(xml_text):
    return base64.b64encode(hashlib.sha256(canonical_for_hash(xml_text)).digest()).decode()


# ---------------- QR ----------------

def _tlv(tag, value):
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    if len(data) > 255:
        raise ValueError(f"قيمة الحقل {tag} في رمز QR أطول من المسموح")
    return bytes([tag, len(data)]) + data


def build_qr(data: InvoiceData, invoice_hash_b64, signature_b64, certificate: CertificateInfo):
    parts = [
        _tlv(1, data.seller.name), _tlv(2, data.seller.vat), _tlv(3, f"{data.issue_date}T{data.issue_time}"),
        _tlv(4, amount(data.tax_inclusive)), _tlv(5, amount(data.tax_amount)),
        _tlv(6, invoice_hash_b64), _tlv(7, signature_b64), _tlv(8, certificate.public_key_der),
    ]
    if data.simplified:
        parts.append(_tlv(9, certificate.signature))  # توقيع الهيئة على الشهادة (للمبسطة فقط)
    return base64.b64encode(b"".join(parts)).decode()


def decode_qr(qr_b64):
    raw, fields, index = base64.b64decode(qr_b64), {}, 0
    while index < len(raw):
        tag, length = raw[index], raw[index + 1]
        fields[tag] = raw[index + 2:index + 2 + length]
        index += 2 + length
    return fields


def extract_qr(xml_text):
    root = etree.fromstring(xml_text.encode())
    found = root.xpath("cac:AdditionalDocumentReference[cbc:ID='QR']/cac:Attachment/cbc:EmbeddedDocumentBinaryObject/text()",
                       namespaces={"cac": NS["cac"], "cbc": NS["cbc"]})
    return found[0] if found else ""


# ---------------- التوقيع ----------------

def _signed_properties(certificate: CertificateInfo, signing_time, for_digest):
    """الخصائص الموقّعة؛ صيغة الهاش تعلن مساحات الأسماء عند استخدامها كما تحسبها مجموعة تطوير الهيئة."""
    ds = f' xmlns:ds="{DS_NS}"' if for_digest else ""
    head = f'<xades:SignedProperties xmlns:xades="{XADES_NS}" Id="xadesSignedProperties">' if for_digest \
        else '<xades:SignedProperties Id="xadesSignedProperties">'
    return (
        f"{head}<xades:SignedSignatureProperties><xades:SigningTime>{signing_time}</xades:SigningTime>"
        "<xades:SigningCertificate><xades:Cert><xades:CertDigest>"
        f'<ds:DigestMethod{ds} Algorithm="{SHA256_URI}"/><ds:DigestValue{ds}>{certificate.digest}</ds:DigestValue>'
        "</xades:CertDigest><xades:IssuerSerial>"
        f"<ds:X509IssuerName{ds}>{escape(certificate.issuer)}</ds:X509IssuerName>"
        f"<ds:X509SerialNumber{ds}>{certificate.serial_number}</ds:X509SerialNumber>"
        "</xades:IssuerSerial></xades:Cert></xades:SigningCertificate></xades:SignedSignatureProperties>"
        "</xades:SignedProperties>"
    )


def signed_properties_digest(certificate, signing_time):
    text = _signed_properties(certificate, signing_time, for_digest=True)
    return base64.b64encode(hashlib.sha256(text.encode()).hexdigest().encode()).decode()


def _ubl_extension(invoice_hash_b64, signature_b64, certificate, signing_time):
    xpath = "http://www.w3.org/TR/1999/REC-xpath-19991116"
    transforms = "".join(
        f'<ds:Transform Algorithm="{xpath}"><ds:XPath>{expr}</ds:XPath></ds:Transform>'
        for expr in ("not(//ancestor-or-self::ext:UBLExtensions)", "not(//ancestor-or-self::cac:Signature)",
                     "not(//ancestor-or-self::cac:AdditionalDocumentReference[cbc:ID='QR'])")
    )
    signed_info = (
        f'<ds:SignedInfo><ds:CanonicalizationMethod Algorithm="{C14N11_URI}"/>'
        '<ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"/>'
        f'<ds:Reference Id="invoiceSignedData" URI=""><ds:Transforms>{transforms}'
        f'<ds:Transform Algorithm="{C14N11_URI}"/></ds:Transforms>'
        f'<ds:DigestMethod Algorithm="{SHA256_URI}"/><ds:DigestValue>{invoice_hash_b64}</ds:DigestValue></ds:Reference>'
        '<ds:Reference Type="http://www.w3.org/2000/09/xmldsig#SignatureProperties" URI="#xadesSignedProperties">'
        f'<ds:DigestMethod Algorithm="{SHA256_URI}"/>'
        f'<ds:DigestValue>{signed_properties_digest(certificate, signing_time)}</ds:DigestValue></ds:Reference>'
        "</ds:SignedInfo>"
    )
    return (
        "<ext:UBLExtension><ext:ExtensionURI>urn:oasis:names:specification:ubl:dsig:enveloped:xades</ext:ExtensionURI>"
        f'<ext:ExtensionContent><sig:UBLDocumentSignatures xmlns:sig="{SIG_NS}" xmlns:sac="{SAC_NS}" xmlns:sbc="{SBC_NS}">'
        "<sac:SignatureInformation><cbc:ID>urn:oasis:names:specification:ubl:signature:1</cbc:ID>"
        "<sbc:ReferencedSignatureID>urn:oasis:names:specification:ubl:signature:Invoice</sbc:ReferencedSignatureID>"
        f'<ds:Signature xmlns:ds="{DS_NS}" Id="signature">{signed_info}'
        f"<ds:SignatureValue>{signature_b64}</ds:SignatureValue>"
        f"<ds:KeyInfo><ds:X509Data><ds:X509Certificate>{certificate.body}</ds:X509Certificate></ds:X509Data></ds:KeyInfo>"
        f'<ds:Object><xades:QualifyingProperties xmlns:xades="{XADES_NS}" Target="signature">'
        f"{_signed_properties(certificate, signing_time, for_digest=False)}</xades:QualifyingProperties></ds:Object>"
        "</ds:Signature></sac:SignatureInformation></sig:UBLDocumentSignatures></ext:ExtensionContent></ext:UBLExtension>"
    )


@dataclass
class SignedInvoice:
    xml: str
    invoice_hash: str
    signature: str
    qr: str


def sign_invoice(data: InvoiceData, private_key_pem, certificate: CertificateInfo, signing_time):
    unsigned = build_unsigned_xml(data)
    hash_b64 = invoice_hash(unsigned)
    signature = sign_invoice_hash(private_key_pem, hash_b64)
    qr = build_qr(data, hash_b64, signature, certificate)
    placeholder = "<ext:UBLExtensions/>"
    if unsigned.count(placeholder) != 1 or unsigned.count(QR_PLACEHOLDER) != 1:
        raise ValueError("بنية XML غير متوقعة أثناء التوقيع.")
    signed = unsigned.replace(placeholder, f"<ext:UBLExtensions>{_ubl_extension(hash_b64, signature, certificate, signing_time)}</ext:UBLExtensions>")
    signed = signed.replace(QR_PLACEHOLDER, qr)
    # تحقق ذاتي: إدراج التوقيع و QR يجب ألا يغيّر الهاش؛ وإلا سترفض الهيئة الفاتورة
    if invoice_hash(signed) != hash_b64:
        raise ValueError("تغيّر هاش الفاتورة بعد إدراج التوقيع؛ أوقفنا الإصدار لمنع إرسال فاتورة غير صحيحة.")
    return SignedInvoice(xml=signed, invoice_hash=hash_b64, signature=signature, qr=qr)
