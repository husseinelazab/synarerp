from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def acct(value):
    """صيغة القوائم المالية: فواصل الآلاف، السالب بين قوسين، والصفر شرطة."""
    try:
        value = Decimal(value or 0).quantize(Decimal("0.01"))
    except Exception:
        return value
    if not value:
        return "-"
    text = f"{abs(value):,.2f}"
    return f"({text})" if value < 0 else text


@register.filter
def get(mapping, key):
    return mapping.get(key) if mapping else None
