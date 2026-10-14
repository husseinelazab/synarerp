from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import style_form
from messaging.models import DEFAULT_TEMPLATES, PLACEHOLDERS, Event, MessageLog, MessageTemplate, MessagingSettings


class SettingsForm(forms.ModelForm):
    token = forms.CharField(label="رمز الوصول / API Key", required=False,
                            widget=forms.PasswordInput(render_value=False),
                            help_text="يُحفظ مشفراً. اتركه فارغاً للإبقاء على المحفوظ")

    class Meta:
        model = MessagingSettings
        fields = ["enabled", "provider", "sender", "account", "token", "api_url", "http_body", "whatsapp_template",
                  "whatsapp_language", "country_code", "auto_invoice", "auto_payment", "auto_overdue",
                  "overdue_every_days"]
        widgets = {"http_body": forms.Textarea(attrs={"rows": 3, "dir": "ltr"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for event in Event:
            self.fields[f"tpl_{event.value}"] = forms.CharField(
                label=event.label, required=False, widget=forms.Textarea(attrs={"rows": 3}),
                initial=MessageTemplate.text_for(self.instance.company, event))
        if self.instance.has_token:
            self.fields["token"].widget.attrs["placeholder"] = "•••••••• (محفوظ)"
        style_form(self)

    def clean(self):
        data = super().clean()
        if data.get("enabled"):
            probe = self.instance
            for f in ("provider", "sender", "account", "api_url"):
                setattr(probe, f, data.get(f) or "")
            if data.get("token"):
                from core.mail import encrypt_password

                probe.token_encrypted = encrypt_password(data["token"])
            if not probe.is_configured:
                raise forms.ValidationError("لتفعيل الإرسال أكمل بيانات المزوّد (المرسل والرمز وما يطلبه المزوّد).")
        return data

    def save(self, commit=True):
        from core.mail import encrypt_password

        obj = super().save(commit=False)
        if self.cleaned_data.get("token"):
            obj.token_encrypted = encrypt_password(self.cleaned_data["token"])
        obj.save()
        for event in Event:
            body = (self.cleaned_data.get(f"tpl_{event.value}") or "").strip() or DEFAULT_TEMPLATES[event]
            MessageTemplate.all_objects.update_or_create(company=obj.company, event=event, defaults={"body": body})
        return obj


@login_required
def settings_page(request):
    from messaging.service import send_test

    cfg = MessagingSettings.for_company(request.company)
    form = SettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST":
        if request.POST.get("action") == "test":
            ok, info = send_test(cfg, request.POST.get("test_to", ""), request.user)
            (messages.success if ok else messages.error)(request, "نجح الاختبار." if ok else f"فشل الاختبار: {info}")
            return redirect("messaging_settings")
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ إعدادات الرسائل." + (" الإرسال عبر المزوّد مفعّل." if cfg.enabled else
                                                                     " الإرسال عبر المزوّد غير مفعّل."))
            return redirect("messaging_settings")
    return render(request, "messaging/settings.html", {"form": form, "cfg": cfg, "placeholders": PLACEHOLDERS,
                                                       "events": list(Event)})


@login_required
def message_log(request):
    entries = MessageLog.objects.select_related("customer", "user")
    status = request.GET.get("status", "")
    if status:
        entries = entries.filter(status=status)
    return render(request, "messaging/log.html", {"entries": entries[:500], "status": status,
                                                  "statuses": MessageLog.STATUS})


@login_required
@require_POST
def send_view(request, kind, pk):
    """إرسال رسالة من صفحة فاتورة أو عميل: mode=link يفتح واتساب، mode=provider عبر المزوّد."""
    from messaging.service import compose, invoice_context, send, statement_context, with_invoice_link
    from selling.models import Customer, SalesInvoice

    mode = "link" if request.POST.get("mode") == "link" else "provider"
    if kind == "invoice":
        doc = get_object_or_404(SalesInvoice, pk=pk, docstatus=1)
        customer, back = doc.customer, doc.get_absolute_url()
        from django.utils import timezone

        overdue = doc.due_date and doc.outstanding_amount > 0 and doc.due_date < timezone.localdate()
        event = "overdue" if overdue else "invoice"
        body, reference = compose(request.company, event, **invoice_context(doc)), doc.name
    else:
        customer = get_object_or_404(Customer, pk=pk)
        back, event = f"/app/customer/{customer.pk}/", "statement"
        body, reference = compose(request.company, event, **statement_context(customer)), customer.name
    body = request.POST.get("body", "").strip() or body
    if kind == "invoice":
        body = with_invoice_link(request.company, body, doc, request)
    try:
        entry, link = send(request.company, customer.phone, body, event=event, reference=reference,
                           customer=customer, user=request.user, mode=mode)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    if link:
        return redirect(link)
    messages.success(request, f"أُرسلت الرسالة إلى {entry.phone} (تابع حالتها في سجل الرسائل).")
    return redirect(back)
