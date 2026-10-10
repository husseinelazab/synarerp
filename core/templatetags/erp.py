from django import template

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
