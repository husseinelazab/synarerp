from django import forms
from django.db import models
from django.forms import inlineformset_factory, modelform_factory


def formfield_callback(db_field, **kwargs):
    if isinstance(db_field, models.DateField):
        kwargs["widget"] = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    elif isinstance(db_field, models.TimeField):
        kwargs["widget"] = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    elif isinstance(db_field, models.TextField):
        kwargs["widget"] = forms.Textarea(attrs={"rows": 2})
    field = db_field.formfield(**kwargs)
    if field is not None and isinstance(db_field, models.ForeignKey):
        field.empty_label = "— اختر —"
        if db_field.name == "uom" and any(f.name == "conversion_factor" for f in db_field.model._meta.fields):
            field.empty_label = "الأساسية"  # سطر بلا وحدة = الوحدة الأساسية للصنف
    return field


def style_form(form):
    from core.remote_select import make_remote

    for name, field in form.fields.items():
        make_remote(field)  # القوائم الكبيرة تُبحث على الخادم بدل تحميل كل الخيارات
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check")
        elif isinstance(widget, forms.CheckboxSelectMultiple):
            continue  # مربعات اختيار متعددة تُنسق كشبكة (.field-multi) لا كحقل إدخال
        else:
            css = "form-control"
            if isinstance(widget, forms.Select):
                css = "form-select"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()
            widget.attrs.setdefault("data-field", name)
    return form


def build_form_class(doctype):
    if doctype.form_class:
        return doctype.form_class
    return modelform_factory(
        doctype.model,
        fields=doctype.form_fields or "__all__",
        formfield_callback=formfield_callback,
    )


def build_formset_class(doctype):
    child = doctype.child
    return inlineformset_factory(
        doctype.model,
        child.model,
        fk_name=child.fk_name,
        fields=list(child.fields),
        extra=0,
        min_num=child.min_rows,
        can_delete=True,
        formfield_callback=formfield_callback,
    )


class EmailSettingsForm(forms.ModelForm):
    password = forms.CharField(label="كلمة المرور", required=False, widget=forms.PasswordInput(render_value=False),
                               help_text="اتركها فارغة للإبقاء على كلمة المرور المحفوظة. لحساب Gmail أو Microsoft 365 "
                                         "استخدم «كلمة مرور التطبيقات» (App Password).")
    clear_password = forms.BooleanField(label="حذف كلمة المرور المحفوظة", required=False)

    class Meta:
        from core.models import EmailSettings

        model = EmailSettings
        fields = ["enabled", "host", "port", "security", "username", "password", "clear_password", "from_email",
                  "from_name", "site_url"]

    def __init__(self, *args, platform=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not platform:
            self.fields["enabled"].label = "استخدام خادم بريد خاص بالشركة"
        if self.instance.has_password:
            self.fields["password"].widget.attrs["placeholder"] = "•••••••• (محفوظة)"
        else:
            del self.fields["clear_password"]
        style_form(self)

    def clean(self):
        data = super().clean()
        if data.get("enabled"):
            missing = [self.fields[f].label for f in ("host", "from_email") if not data.get(f)]
            if missing:
                raise forms.ValidationError("لتفعيل الإرسال أدخل: " + "، ".join(missing))
        return data

    def save(self, commit=True):
        from core.mail import encrypt_password

        obj = super().save(commit=False)
        if self.cleaned_data.get("password"):
            obj.password_encrypted = encrypt_password(self.cleaned_data["password"])
        elif self.cleaned_data.get("clear_password"):
            obj.password_encrypted = ""
        if commit:
            obj.save()
        return obj


class PrintSettingsForm(forms.ModelForm):
    letterhead_file = forms.FileField(label="كليشة الجمعية (رأس الخطاب)", required=False,
                                     help_text="صورة PNG أو JPG أو WebP حتى 500 كيلوبايت، بعرض ورقة الخطاب.")
    remove_letterhead = forms.BooleanField(label="حذف الكليشة الحالية", required=False)
    logo_file = forms.FileField(label="الشعار", required=False,
                                help_text="صورة PNG أو JPG أو WebP بحجم 500 كيلوبايت كحد أقصى. يُفضّل خلفية شفافة.")
    remove_logo = forms.BooleanField(label="حذف الشعار الحالي", required=False)

    LOGO_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}
    MAX_LOGO = 500 * 1024

    class Meta:
        from core.models import PrintSettings

        model = PrintSettings
        fields = ["template", "accent_color", "name_en", "address_en", "bilingual", "show_amount_in_words",
                  "show_signatures", "show_item_code", "show_bank_details", "bank_name", "bank_account_name", "iban",
                  "footer_text", "sales_terms", "letter_opening", "letter_layout"]
        widgets = {"accent_color": forms.TextInput(attrs={"type": "color", "style": "height:44px;width:120px;padding:4px"}),
                   "sales_terms": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["letter_layout"].required = False
        if not self.instance.logo:
            del self.fields["remove_logo"]
        style_form(self)

    def clean_letter_layout(self):
        return self.cleaned_data.get("letter_layout") or "unified"

    def clean_accent_color(self):
        import re

        color = self.cleaned_data["accent_color"].strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise forms.ValidationError("أدخل لوناً بصيغة #RRGGBB.")
        return color.lower()

    def clean_iban(self):
        iban = self.cleaned_data["iban"].replace(" ", "").upper()
        if iban and not (iban[:2].isalpha() and iban[2:].isalnum() and 15 <= len(iban) <= 34):
            raise forms.ValidationError("رقم الآيبان غير صحيح (مثل SA0380000000608010167519).")
        return iban

    def clean_logo_file(self):
        return self._clean_brand_image(self.cleaned_data.get("logo_file"))

    def clean_letterhead_file(self):
        return self._clean_brand_image(self.cleaned_data.get("letterhead_file"))

    def _clean_brand_image(self, upload):
        import base64
        if not upload:
            return None
        if upload.size > self.MAX_LOGO:
            raise forms.ValidationError("حجم الشعار أكبر من 500 كيلوبايت.")
        data = upload.read()
        # نتحقق من نوع الملف من محتواه لا من امتداده؛ لا نقبل SVG لأنه قد يحمل نصوصاً برمجية
        mime = next((m for sig, m in self.LOGO_TYPES.items() if data.startswith(sig)), None)
        if mime == "image/webp" and data[8:12] != b"WEBP":
            mime = None
        if mime is None:
            raise forms.ValidationError("صيغة غير مدعومة. ارفع صورة PNG أو JPG أو WebP.")
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("remove_letterhead"):
            obj.letterhead = ""
        if self.cleaned_data.get("letterhead_file"):
            obj.letterhead = self.cleaned_data["letterhead_file"]
        if self.cleaned_data.get("remove_logo"):
            obj.logo = ""
        if self.cleaned_data.get("logo_file"):
            obj.logo = self.cleaned_data["logo_file"]
        if commit:
            obj.save()
        return obj
