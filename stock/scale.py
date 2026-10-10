"""الموازين: قراءة باركود الميزان، وتجهيز قائمة الأصناف (PLU) وأسعارها للبرنامج الوسيط في الفرع."""
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from core.tenancy import get_current_company


def read_scale_barcode(code, company=None):
    """(الصنف، الكمية) من باركود الميزان، أو (None، None)."""
    from core.models import Company
    from stock.models import Item, ScaleSettings, ScaleValueType

    company = company or get_current_company() or Company.current()
    if company is None:
        return None, None
    settings = ScaleSettings.objects.filter(company=company).first()
    parsed = settings.parse(code) if settings else None
    if parsed is None:
        return None, None
    plu, value = parsed
    item = Item.objects.filter(plu=plu, is_weighed=True, disabled=False).first()
    if item is None:
        return None, None
    if settings.value_type == ScaleValueType.WEIGHT:
        return item, (Decimal(value) / 1000).quantize(Decimal("0.001"))
    # الباركود يحمل السعر: الكمية = السعر ÷ سعر الكيلو شاملاً الضريبة
    unit = unit_price(item, company)
    if not unit:
        return None, None
    return item, (Decimal(value) / 100 / unit).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def unit_price(item, company):
    """سعر الوحدة (الكيلو) للعميل شاملاً الضريبة كما يُرسل للميزان."""
    from stock.tools import prices_include_tax_default, shelf_price

    return shelf_price(item, item.selling_rate, prices_include_tax_default(), company)


def plu_list(scale, company):
    rows = []
    for item in scale.items():
        rows.append({
            "plu": item.plu, "code": item.code, "name": item.name, "scale_name": item.scale_name or item.name,
            "price": str(unit_price(item, company)), "unit": "kg", "uom": str(item.uom),
            "shelf_life_days": item.shelf_life_days, "barcode": item.barcode, "group": str(item.item_group),
        })
    return rows


def version_of(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def scales_payload(company):
    """كل ما يحتاجه البرنامج الوسيط: صيغة الباركود، والموازين، وأصناف كل ميزان بأسعارها، ونسخة للمقارنة."""
    from stock.models import Scale, ScaleSettings

    settings = ScaleSettings.for_company(company)
    scales = []
    for scale in Scale.objects.filter(enabled=True).prefetch_related("item_groups"):
        plus = plu_list(scale, company)
        scales.append({
            "id": scale.id, "name": scale.name, "brand": scale.brand, "driver": scale.driver,
            "ip": scale.ip_address, "port": scale.port, "version": version_of(plus), "plus": plus,
        })
    payload = {
        "company": company.name, "currency": company.currency.code,
        "barcode": {"prefix": settings.prefix_list[0] if settings.prefix_list else "21",
                    "plu_length": settings.plu_length, "value_type": settings.value_type,
                    "value_length": settings.value_length},
        "scales": scales,
    }
    payload["version"] = version_of(payload)
    return payload
