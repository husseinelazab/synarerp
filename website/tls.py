"""فحص النطاق قبل إصدار شهادة HTTPS تلقائياً (Caddy on_demand_tls): لا تُصدر شهادة إلا لنطاق موقع منشور."""
from django.http import HttpResponse, HttpResponseNotFound

from website.models import normalize_domain
from website.site import domain_map


def domain_check(request):
    domain = normalize_domain(request.GET.get("domain", ""))
    if domain and domain in domain_map():
        return HttpResponse("ok")
    return HttpResponseNotFound("unknown")
