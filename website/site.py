"""تحديد موقع الجمعية من الطلب: بالنطاق المستقل (albirr.org.sa) أو بمسار المعاينة على المنصة (/site/<المعرّف>/)."""
import functools
import time

from django.http import Http404
from django.urls import reverse

from core.tenancy import tenant_context

_DOMAINS = {"at": 0.0, "map": {}}
CACHE_SECONDS = 60
SITE_URLCONF = "website.site_urls"


def domain_map():
    """{النطاق: معرّف الموقع} للمواقع المنشورة، مخزّن دقيقة لتخفيف الاستعلامات."""
    if time.monotonic() - _DOMAINS["at"] > CACHE_SECONDS:
        from website.models import Website

        _DOMAINS["map"] = dict(Website.all_objects.filter(is_published=True).exclude(domain=None)
                               .values_list("domain", "pk"))
        _DOMAINS["at"] = time.monotonic()
    return _DOMAINS["map"]


def clear_cache():
    _DOMAINS["at"] = 0.0


def host_of(request):
    host = request.META.get("HTTP_X_FORWARDED_HOST") or request.META.get("HTTP_HOST", "")
    host = host.split(",")[0].strip().lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def can_preview(request, website):
    """مستخدمو الجمعية يرون موقعها قبل نشره."""
    user = request.user
    return user.is_authenticated and (user.is_superuser or user.memberships.filter(
        company=website.company, is_active=True).exists())


def site_view(view):
    """يربط العرض بموقع الجمعية: يحدد الموقع، ويرفض غير المنشور لغير مستخدميها، وينفذ داخل سياق الجمعية."""

    @functools.wraps(view)
    def wrapper(request, *args, site_slug=None, **kwargs):
        from website.models import Website

        website = getattr(request, "site", None)
        if website is None:
            website = Website.all_objects.select_related("company").filter(slug=site_slug).first()
            if website is None:
                raise Http404
            request.site_prefix = f"/site/{website.slug}"
        else:
            request.site_prefix = ""
        if not website.is_published and not can_preview(request, website):
            raise Http404
        company = website.company
        subscription = getattr(company, "subscription", None)
        if not company.is_nonprofit or subscription is None or not subscription.is_valid:
            raise Http404
        request.site = website
        from core.models import PrintSettings

        request.site_brand = PrintSettings.for_company(company)
        with tenant_context(company):
            return view(request, *args, **kwargs)

    return wrapper


def site_url(request, name, *args):
    """رابط صفحة في موقع الجمعية يعمل على النطاق المستقل وفي المعاينة."""
    return getattr(request, "site_prefix", "") + reverse(name, args=args, urlconf=SITE_URLCONF)


def render_public(request, standalone, body, title, context, parent=""):
    """صفحة عامة لوحدة (تسجيل برنامج، تطوع، استبيان، تحقق شهادة): داخل إطار موقع الجمعية متى فُتحت من نطاقه
    أو معاينته، وإلا بالقالب المستقل على رابط المنصة."""
    from django.shortcuts import render

    if getattr(request, "site", None) is None:
        return render(request, standalone, context)
    return render(request, "website/embedded.html",
                  dict(context, embed_body=body, embed_title=title, embed_parent=parent))
