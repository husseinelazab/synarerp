from django.conf import settings
from django.http import HttpResponseBadRequest

from website.site import SITE_URLCONF, domain_map, host_of


def is_platform_host(host):
    return any(host == h or host.endswith("." + h) for h in settings.PLATFORM_HOSTS)


class SiteHostMiddleware:
    """طلب على النطاق المستقل لجمعية يُخدم من مسارات موقعها (website.site_urls) بدلاً من النظام.

    عند تفعيل SYNAR_DYNAMIC_SITE_HOSTS تُقبل نطاقات المواقع المسجلة دون إضافتها يدوياً إلى ALLOWED_HOSTS،
    ويُرفض أي نطاق آخر غير نطاقات المنصة (SYNAR_PLATFORM_HOSTS).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = host_of(request)
        site_id = domain_map().get(host)
        if site_id is not None:
            from website.models import Website

            request.site = Website.all_objects.select_related("company").get(pk=site_id)
            request.urlconf = SITE_URLCONF
        elif getattr(settings, "DYNAMIC_SITE_HOSTS", False) and not is_platform_host(host):
            return HttpResponseBadRequest("Unknown host")
        return self.get_response(request)
