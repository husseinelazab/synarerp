from django import template

from website.site import site_url as _site_url

register = template.Library()


@register.simple_tag(takes_context=True)
def site_url(context, name, *args):
    """{% site_url 'site_about' %}: رابط داخل موقع الجمعية (نطاقها المستقل أو المعاينة)."""
    return _site_url(context["request"], name, *args)
