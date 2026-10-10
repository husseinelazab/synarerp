from django import template

from django.utils.safestring import mark_safe

from core.utils import currency_symbol_html
from core.utils import money as _money

register = template.Library()


@register.filter
def money(value):
    try:
        return _money(value)
    except Exception:
        return value


@register.filter
def get_item(mapping, key):
    return mapping.get(key)


@register.filter
def voucher_label(slug):
    from core.registry import voucher_label as label

    return label(slug)


@register.filter(name="abs")
def abs_filter(value):
    try:
        return abs(value)
    except TypeError:
        return value


@register.filter
def cur(value):
    """مبلغ منسق مع رمز عملة الشركة."""
    try:
        text = _money(value)
    except Exception:
        return value
    return mark_safe(f'<span class="amount"><bdi dir="ltr">{text}</bdi>{currency_symbol_html()}</span>')


@register.simple_tag
def currency_symbol():
    return mark_safe(currency_symbol_html())
