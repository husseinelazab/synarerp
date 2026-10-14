"""إعدادات موقع الجمعية من داخل النظام."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from website.forms import WebsiteForm
from website.models import Website
from website.site import clear_cache


@login_required
def website_settings(request):
    site = Website.for_company(request.company)
    form = WebsiteForm(request.POST or None, request.FILES or None, instance=site)
    if request.method == "POST" and form.is_valid():
        form.save()
        clear_cache()
        messages.success(request, "حُفظت إعدادات الموقع.")
        return redirect("website_settings")
    return render(request, "website/settings.html", {"title": "إعدادات الموقع الإلكتروني", "form": form, "site": site})
