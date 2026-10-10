"""رمز QR للفاتورة الضريبية المبسطة وفق متطلبات هيئة الزكاة والضريبة والجمارك (المرحلة الأولى).

الحقول بترميز TLV ثم Base64: اسم البائع، الرقم الضريبي، وقت الفاتورة، الإجمالي شامل الضريبة، مبلغ الضريبة.
"""
import base64
import datetime

from django.utils import timezone


def _tlv(tag, value):
    data = str(value).encode("utf-8")[:255]
    return bytes([tag, len(data)]) + data


def qr_payload(seller_name, vat_number, invoice_time, total_with_vat, vat_total):
    if isinstance(invoice_time, datetime.datetime):
        invoice_time = timezone.localtime(invoice_time).replace(microsecond=0).isoformat()
    raw = b"".join([
        _tlv(1, seller_name),
        _tlv(2, vat_number),
        _tlv(3, invoice_time),
        _tlv(4, f"{total_with_vat:.2f}"),
        _tlv(5, f"{vat_total:.2f}"),
    ])
    return base64.b64encode(raw).decode("ascii")


def decode_payload(payload):
    raw, fields, i = base64.b64decode(payload), {}, 0
    while i < len(raw):
        tag, length = raw[i], raw[i + 1]
        fields[tag] = raw[i + 2:i + 2 + length].decode("utf-8")
        i += 2 + length
    return fields


def qr_svg(data):
    """يعيد رمز QR بصيغة SVG، أو نصاً فارغاً إن لم تكن مكتبة qrcode مثبتة."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    return image.to_string(encoding="unicode")


def invoice_qr_svg(invoice):
    company = invoice.company
    if not company.tax_id:
        return ""
    # الإشعار الدائن يُعرض بالقيم المطلقة
    return qr_svg(qr_payload(company.name, company.tax_id, invoice.created_at,
                             abs(invoice.grand_total), abs(invoice.tax_amount)))
