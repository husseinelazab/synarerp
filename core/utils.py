from decimal import ROUND_HALF_UP, Decimal


def flt(value, precision=2):
    """تحويل أي قيمة إلى Decimal مقرّب بعدد الخانات المطلوب."""
    if value is None or value == "":
        value = 0
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def money(value):
    """تنسيق المبالغ بفاصل الآلاف وخانتين عشريتين."""
    return f"{flt(value):,.2f}"
