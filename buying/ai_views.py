"""صفحات قراءة فواتير الموردين بالذكاء الاصطناعي: الإعدادات، الرفع، والمراجعة قبل إنشاء المسودة."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from buying.ai import AIError, build_review, check_file, extract_invoice, to_decimal, verify_key
from buying.models import AIInvoiceSettings, PurchaseInvoice, Supplier
from core.forms import style_form

SESSION_KEY = "ai_invoice"


def _plan_allows(request):
    subscription = getattr(request.company, "subscription", None)
    return bool(subscription and subscription.plan.includes_ai)


def _plan_redirect(request):
    messages.warning(request, "قراءة الفواتير بالذكاء الاصطناعي متاحة في الباقتين الاحترافية والمؤسسات. "
                              "يمكنك الترقية من صفحة الاشتراك.")
    return redirect("billing" if request.membership.can_manage else "dashboard")


class AISettingsForm(forms.ModelForm):
    api_key = forms.CharField(label="مفتاح Anthropic API", required=False,
                              widget=forms.PasswordInput(render_value=False, attrs={"dir": "ltr", "autocomplete": "off"}),
                              help_text="من console.anthropic.com ← API Keys. يُحفظ مشفراً، اتركه فارغاً للإبقاء على المحفوظ.")
    clear_key = forms.BooleanField(label="حذف المفتاح المحفوظ", required=False)

    class Meta:
        model = AIInvoiceSettings
        fields = ["model", "consent", "enabled"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.has_key:
            self.fields["api_key"].widget.attrs["placeholder"] = "•••••••• (محفوظ)"
        else:
            del self.fields["clear_key"]
        style_form(self)

    def clean_api_key(self):
        key = self.cleaned_data["api_key"].strip()
        if key and not key.startswith("sk-ant-"):
            raise forms.ValidationError("مفتاح Anthropic يبدأ بـ sk-ant-. انسخه كاملاً من console.anthropic.com.")
        return key

    def clean(self):
        data = super().clean()
        if data.get("enabled"):
            has_key = (self.instance.has_key and not data.get("clear_key")) or data.get("api_key")
            if not has_key:
                raise forms.ValidationError("أدخل مفتاح API قبل التفعيل.")
            if not data.get("consent"):
                raise forms.ValidationError("التفعيل يتطلب الموافقة على إرسال ملفات الفواتير إلى Anthropic.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("clear_key"):
            obj.set_api_key("")
            obj.enabled = False
        if self.cleaned_data.get("api_key"):
            obj.set_api_key(self.cleaned_data["api_key"])
        obj.save()
        return obj


@login_required
def ai_settings(request):
    if not _plan_allows(request):
        return _plan_redirect(request)
    cfg = AIInvoiceSettings.for_company(request.company)
    if request.method == "POST" and request.POST.get("action") == "test":
        if not cfg.has_key:
            messages.error(request, "احفظ مفتاح API أولاً ثم اختبره.")
        else:
            try:
                verify_key(cfg.api_key, cfg.model)
                messages.success(request, f"المفتاح يعمل والنموذج {cfg.get_model_display()} متاح لحسابك.")
            except AIError as exc:
                messages.error(request, str(exc))
        return redirect("ai_settings")
    form = AISettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ إعدادات الذكاء الاصطناعي.")
        return redirect("ai_settings")
    return render(request, "buying/ai_settings.html", {"form": form, "cfg": cfg})


@login_required
def ai_invoice_upload(request):
    if not _plan_allows(request):
        return _plan_redirect(request)
    cfg = AIInvoiceSettings.for_company(request.company)
    if request.method == "POST" and cfg.active:
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "اختر صورة الفاتورة أو ملف PDF.")
            return redirect("ai_invoice_upload")
        data = upload.read()
        try:
            mime = check_file(data)
            result = extract_invoice(cfg.api_key, cfg.model, data, mime)
        except AIError as exc:
            messages.error(request, str(exc))
            return redirect("ai_invoice_upload")
        AIInvoiceSettings.all_objects.filter(pk=cfg.pk).update(invoices_read=F("invoices_read") + 1,
                                                               last_used_at=timezone.now())
        request.session[SESSION_KEY] = {"result": result, "file_name": upload.name[:100]}
        return redirect("ai_invoice_review")
    return render(request, "buying/ai_upload.html", {"cfg": cfg})


def _review_context(stored, review, form_values=None):
    """عند إعادة العرض بعد خطأ تبقى اختيارات المستخدم (البند، الصنف، الكمية، السعر) كما أدخلها."""
    from stock.models import Item

    for index, row in enumerate(review["rows"]):
        row["checked"] = True
        if form_values:
            row["checked"] = bool(form_values.get(f"include_{index}"))
            row["item_id"] = int(form_values.get(f"item_{index}") or 0) or None
            row["qty"] = form_values.get(f"qty_{index}", row["qty"])
            row["rate"] = form_values.get(f"rate_{index}", row["rate"])
    return {
        "result": stored["result"], "file_name": stored["file_name"], "review": review,
        "suppliers": Supplier.objects.filter(disabled=False).only("id", "name"),
        "items": Item.objects.filter(disabled=False).only("id", "code", "name").order_by("name"),
        "values": form_values or {},
    }


@login_required
def ai_invoice_review(request):
    if not _plan_allows(request):
        return _plan_redirect(request)
    stored = request.session.get(SESSION_KEY)
    if not stored:
        messages.info(request, "ارفع فاتورة المورد أولاً.")
        return redirect("ai_invoice_upload")
    review = build_review(stored["result"])
    if request.method == "POST":
        if request.POST.get("action") == "discard":
            request.session.pop(SESSION_KEY, None)
            messages.info(request, "تم تجاهل الفاتورة المقروءة.")
            return redirect("ai_invoice_upload")
        errors, invoice = _create_draft(request, stored, review)
        if invoice:
            request.session.pop(SESSION_KEY, None)
            messages.success(request, f"تم إنشاء {invoice.name} كمسودة. راجع المستودع والضريبة ثم اعتمدها.")
            return redirect(reverse("doc_detail", args=["purchase-invoice", invoice.pk]))
        for error in errors:
            messages.error(request, error)
        return render(request, "buying/ai_review.html", _review_context(stored, review, request.POST))
    return render(request, "buying/ai_review.html", _review_context(stored, review))


def _create_draft(request, stored, review):
    """ينشئ فاتورة مشتريات «مسودة» من اختيارات المستخدم في صفحة المراجعة. يعيد (الأخطاء، الفاتورة)."""
    from stock.models import Item, Warehouse

    post, errors = request.POST, []
    supplier = None
    if post.get("supplier") == "new":
        name = post.get("new_supplier_name", "").strip()
        if not name:
            errors.append("اكتب اسم المورد الجديد.")
        elif Supplier.objects.filter(name__iexact=name).exists():
            errors.append(f"يوجد مورد باسم «{name}» بالفعل؛ اختره من القائمة.")
    else:
        supplier = Supplier.objects.filter(pk=post.get("supplier") or 0, disabled=False).first()
        if supplier is None:
            errors.append("اختر المورد أو أنشئ مورداً جديداً.")

    rows = []
    for index, row in enumerate(review["rows"]):
        if not post.get(f"include_{index}"):
            continue
        item = Item.objects.filter(pk=post.get(f"item_{index}") or 0, disabled=False).first()
        label = row["description"] or f"البند {index + 1}"
        if item is None:
            errors.append(f"اختر الصنف المقابل لـ «{label}» أو ألغِ تحديد البند.")
            continue
        qty, rate = to_decimal(post.get(f"qty_{index}"), 3), to_decimal(post.get(f"rate_{index}"))
        if qty <= 0 or rate < 0:
            errors.append(f"الكمية والسعر غير صحيحين في «{label}».")
            continue
        # وحدة باركود الكرتون مثلاً تبقى فقط إن لم يغيّر المستخدم الصنف المطابق
        uom_id = row["uom_id"] if row["item_id"] == item.pk else None
        rows.append((item, uom_id, qty, rate))
    if not rows and not errors:
        errors.append("حدد بنداً واحداً على الأقل.")
    if errors:
        return errors, None

    with transaction.atomic():
        if supplier is None:
            supplier = Supplier.objects.create(name=post["new_supplier_name"].strip(),
                                               tax_id=post.get("new_supplier_tax_id", "").strip()[:20])
        invoice = PurchaseInvoice(
            supplier=supplier, bill_no=post.get("bill_no", "").strip()[:50],
            posting_date=parse_date(post.get("posting_date") or "") or timezone.localdate(),
            warehouse=Warehouse.objects.filter(disabled=False).first(),
            remarks=f"أُنشئت بقراءة الملف «{stored['file_name']}» بالذكاء الاصطناعي؛ راجعها قبل الاعتماد.",
            created_by=request.user,
        )
        if review["tax_rate"] > 0:
            invoice.tax_rate = review["tax_rate"]
        invoice.save()
        for item, uom_id, qty, rate in rows:
            invoice.items.create(item=item, uom_id=uom_id, qty=qty, rate=rate)
        invoice.calculate_totals()
        invoice.save()
    return [], invoice
