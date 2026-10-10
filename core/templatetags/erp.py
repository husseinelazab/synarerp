from django import template

from django.utils.safestring import mark_safe
from django.utils.html import format_html

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
def cur(value, currency=None):
    """مبلغ منسق مع رمز العملة (عملة المستند إن مُررت، وإلا عملة الشركة)."""
    try:
        text = _money(value)
    except Exception:
        return value
    return mark_safe(f'<span class="amount"><bdi dir="ltr">{text}</bdi>{currency_symbol_html(currency or None)}</span>')


@register.simple_tag
def currency_symbol():
    return mark_safe(currency_symbol_html())


@register.filter
def plain_number(value):
    """رقم بدون أصفار زائدة وبالنقطة العشرية (لأسعار الصرف)."""
    try:
        return f"{value.normalize():f}"
    except AttributeError:
        return value


@register.filter
def hijri(value):
    """التاريخ الهجري (أم القرى) لتاريخ ميلادي."""
    from core.hijri import format_hijri

    return format_hijri(value) if value else ""


@register.filter
def sub_money(value, arg):
    """طرح مبلغين وتنسيق الناتج (بديل آمن لـ add الذي يقرّب الكسور)."""
    from decimal import Decimal

    try:
        return _money(Decimal(str(value)) - Decimal(str(arg)))
    except Exception:
        return ""


@register.simple_tag(takes_context=True)
def en(context, text):
    """الترجمة الإنجليزية للعناوين في المطبوعات عند تفعيلها في إعدادات الطباعة."""
    ps = context.get("ps")
    if ps is not None and ps.bilingual:
        return format_html('<span class="en">{}</span>', text)
    return ""
