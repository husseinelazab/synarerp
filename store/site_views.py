"""صفحات المتجر الإلكتروني للتبرعات على موقع الجمعية."""
from django.shortcuts import render

from website.site import site_view


@site_view
def store_home(request):
    return render(request, "website/placeholder.html", {"page_title": "تبرع الآن"})
