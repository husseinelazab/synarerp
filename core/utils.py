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


def allocate(target, weights):
    """توزيع مبلغ على مفاتيح بنسبة أوزانها مع ترحيل فرق التقريب إلى آخر مفتاح."""
    target = flt(target)
    keys = list(weights)
    if not keys:
        return {}
    total_weight = sum((flt(w) for w in weights.values()), flt(0))
    result, allocated = {}, flt(0)
    for key in keys[:-1]:
        share = flt(target * flt(weights[key]) / total_weight) if total_weight else flt(0)
        result[key] = share
        allocated += share
    result[keys[-1]] = target - allocated
    return result
