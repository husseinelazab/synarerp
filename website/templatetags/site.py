import re

from django import template

from website.site import site_url as _site_url

register = template.Library()


@register.simple_tag(takes_context=True)
def site_url(context, name, *args):
    """{% site_url 'site_about' %}: رابط داخل موقع الجمعية (نطاقها المستقل أو المعاينة)."""
    return _site_url(context["request"], name, *args)


IBAN_RE = re.compile(r"SA[\d\s]{22,30}", re.I)


@register.filter
def bank_line(line):
    """سطر الحساب البنكي «البنك - الآيبان» إلى {bank, iban (مجمّع كل 4 خانات), iban_raw}."""
    match = IBAN_RE.search(line or "")
    if not match:
        return {"bank": line, "iban": "", "iban_raw": ""}
    raw = re.sub(r"\s", "", match.group()).upper()
    bank = (line[:match.start()] + line[match.end():]).strip(" -–—:|")
    return {"bank": bank, "iban": " ".join(raw[i:i + 4] for i in range(0, len(raw), 4)), "iban_raw": raw}


@register.simple_tag(takes_context=True)
def cart_count(context):
    """عدد فرص التبرع في سلة الزائر لرأس الموقع (السلة في الجلسة لكل جمعية)."""
    request = context["request"]
    site = getattr(request, "site", None)
    session = getattr(request, "session", None)
    if site is None or session is None:
        return 0
    return len(session.get(f"store_cart_{site.company_id}", []))
