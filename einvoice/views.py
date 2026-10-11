from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.forms import style_form
from einvoice.models import EInvoice, EInvoiceSettings, EInvoiceStatus, OnboardingStatus
from einvoice.services import activate_production, run_compliance_checks, start_onboarding, submit_einvoice


class SettingsForm(forms.ModelForm):
    class Meta:
        model = EInvoiceSettings
        fields = ["environment", "auto_submit", "seller_name", "vat_number", "crn", "street", "building_number",
                  "district", "city", "postal_code", "branch_name", "industry", "invoice_types"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


def _errors(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _settings(request):
    settings = EInvoiceSettings.objects.first()
    if settings is None:
        company = request.company
        settings = EInvoiceSettings(
            seller_name=company.name, vat_number=company.tax_id, crn=company.commercial_register,
            city="", street="", district="", building_number="", postal_code="",
        )
    return settings


@login_required
def settings_page(request):
    settings = _settings(request)
    form = SettingsForm(request.POST or None, instance=settings)
    if request.method == "POST" and request.POST.get("action") == "save":
        if form.is_valid():
            environment_changed = settings.pk and "environment" in form.changed_data
            saved = form.save()
            if environment_changed:
                saved.status, saved.enabled = OnboardingStatus.NOT_STARTED, False
                saved.save(update_fields=["status", "enabled"])
                messages.warning(request, "تغيّرت البيئة؛ يجب إعادة خطوات الربط.")
            messages.success(request, "تم حفظ بيانات الفوترة الإلكترونية.")
            return redirect("einvoice_settings")
    elif request.method == "POST" and settings.pk:
        action = request.POST.get("action")
        try:
            if action == "compliance":
                start_onboarding(settings, request.POST.get("otp", "").strip())
                messages.success(request, "تم الحصول على شهادة الامتثال. شغّل فحوص الامتثال الآن.")
            elif action == "checks":
                if run_compliance_checks(settings):
                    messages.success(request, "اجتازت كل المستندات التجريبية فحوص الامتثال.")
                else:
                    messages.error(request, "لم تجتز بعض المستندات فحوص الامتثال؛ راجع التفاصيل أدناه.")
            elif action == "activate":
                activate_production(settings)
                messages.success(request, "تم تفعيل الفوترة الإلكترونية. ستُصدر كل فاتورة معتمدة وتُرسل للهيئة تلقائياً.")
            elif action == "toggle":
                settings.enabled = not settings.enabled and settings.status == OnboardingStatus.ACTIVE
                settings.save(update_fields=["enabled"])
        except ValidationError as exc:
            messages.error(request, _errors(exc))
        return redirect("einvoice_settings")
    return render(request, "einvoice/settings.html", {"form": form, "settings": settings,
                                                       "steps": OnboardingStatus})


@login_required
def einvoice_list(request):
    status = request.GET.get("status", "")
    queryset = EInvoice.objects.select_related("sales_invoice", "sales_invoice__customer")
    if status:
        queryset = queryset.filter(status=status)
    counts = dict(EInvoice.objects.values_list("status").annotate(n=Count("id")))
    return render(request, "einvoice/list.html", {
        "einvoices": queryset[:300], "status": status, "statuses": EInvoiceStatus.choices,
        "counts": [(value, label, counts.get(value, 0)) for value, label in EInvoiceStatus.choices],
        "settings": EInvoiceSettings.objects.first(),
    })


@login_required
@require_POST
def resubmit(request, pk):
    einvoice = get_object_or_404(EInvoice, pk=pk)
    if einvoice.is_final:
        messages.info(request, "الفاتورة مقبولة لدى الهيئة بالفعل.")
    else:
        einvoice = submit_einvoice(einvoice)
        if einvoice.is_final:
            messages.success(request, f"تم الإرسال: {einvoice.get_status_display()}.")
        else:
            messages.error(request, f"{einvoice.get_status_display()}: " + " ".join(einvoice.response.get("messages", [])))
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        next_url = "einvoice_list"
    return redirect(next_url)


@login_required
@require_POST
def resubmit_all(request):
    from einvoice.services import submit_pending

    results = submit_pending(request.company)
    accepted = sum(1 for e in results if e.is_final)
    messages.info(request, f"أُعيد إرسال {len(results)} فاتورة، قُبل منها {accepted}.")
    return redirect("einvoice_list")


@login_required
def download_xml(request, pk):
    einvoice = get_object_or_404(EInvoice, pk=pk)
    xml = einvoice.cleared_xml if request.GET.get("cleared") and einvoice.cleared_xml else einvoice.xml
    response = HttpResponse(xml, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{einvoice.sales_invoice.name}.xml"'
    return response
