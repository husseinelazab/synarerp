from django import forms

from core.forms import PrintSettingsForm, style_form
from website.models import Website


class WebsiteForm(forms.ModelForm):
    hero_file = forms.FileField(label="صورة الواجهة", required=False,
                                help_text="PNG أو JPG أو WebP بحجم 800 كيلوبايت كحد أقصى، يُفضّل بعرض 1600 بكسل")
    remove_hero = forms.BooleanField(label="حذف صورة الواجهة", required=False)
    MAX_IMAGE = 800 * 1024

    class Meta:
        model = Website
        fields = ["slug", "domain", "is_published", "tagline", "hero_title", "hero_subtitle", "about", "primary_color",
                  "fundraising_license_number", "fundraising_license_expiry", "bank_accounts", "whatsapp",
                  "working_hours", "map_url", "x_url", "instagram_url", "snapchat_url", "youtube_url", "linkedin_url"]
        widgets = {"primary_color": forms.TextInput(attrs={"type": "color", "style": "height:44px;width:120px;padding:4px"}),
                   "about": forms.Textarea(attrs={"rows": 4}), "bank_accounts": forms.Textarea(attrs={"rows": 3}),
                   "fundraising_license_expiry": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.hero_image:
            del self.fields["remove_hero"]
        style_form(self)

    def clean_hero_file(self):
        upload = self.cleaned_data.get("hero_file")
        if not upload:
            return None
        if upload.size > self.MAX_IMAGE:
            raise forms.ValidationError("حجم الصورة أكبر من 800 كيلوبايت.")
        import base64

        data = upload.read()
        kind = next((t for magic, t in PrintSettingsForm.LOGO_TYPES.items() if data.startswith(magic)), None)
        if kind is None:
            raise forms.ValidationError("الصيغ المسموحة: PNG أو JPG أو WebP.")
        return f"data:{kind};base64,{base64.b64encode(data).decode()}"

    def save(self, commit=True):
        site = super().save(commit=False)
        if self.cleaned_data.get("remove_hero"):
            site.hero_image = ""
        if self.cleaned_data.get("hero_file"):
            site.hero_image = self.cleaned_data["hero_file"]
        if commit:
            site.save()
        return site


# ---------------------------------------------------------------- رفع الصور والملفات من النظام

MAX_IMAGE_BYTES = 800 * 1024
MAX_PHOTOS_PER_UPLOAD = 20
PDF_MAGIC = b"%PDF"


def image_data_uri(upload, max_bytes=MAX_IMAGE_BYTES):
    """يتحقق من صورة مرفوعة (PNG/JPG/WebP بحد أقصى للحجم) ويعيدها data URI تُحفظ في قاعدة البيانات."""
    import base64

    if upload.size > max_bytes:
        raise forms.ValidationError(f"حجم الصورة «{upload.name}» أكبر من {max_bytes // 1024} كيلوبايت.")
    data = upload.read()
    kind = next((t for magic, t in PrintSettingsForm.LOGO_TYPES.items() if data.startswith(magic)), None)
    if kind is None or (kind == "image/webp" and data[8:12] != b"WEBP"):
        raise forms.ValidationError(f"الملف «{upload.name}» ليس صورة PNG أو JPG أو WebP.")
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


class ImageUploadForm(forms.Form):
    file = forms.FileField(label="الصورة", required=False, help_text="PNG أو JPG أو WebP بحجم 800 كيلوبايت كحد أقصى")
    remove = forms.BooleanField(label="حذف الصورة الحالية", required=False)

    def clean_file(self):
        upload = self.cleaned_data.get("file")
        return image_data_uri(upload) if upload else ""


class DocumentUploadForm(forms.Form):
    file = forms.FileField(label="ملف الوثيقة", help_text="PDF أو صورة PNG/JPG بحجم 10 ميجابايت كحد أقصى")

    def clean_file(self):
        from website.models import PublishedDocument

        upload = self.cleaned_data["file"]
        if upload.size > PublishedDocument.MAX_BYTES:
            raise forms.ValidationError("حجم الملف يتجاوز 10 ميجابايت.")
        data = upload.read()
        if data.startswith(PDF_MAGIC):
            content_type = "application/pdf"
        else:
            content_type = next((t for magic, t in PrintSettingsForm.LOGO_TYPES.items()
                                 if data.startswith(magic) and t != "image/webp"), None)
        if content_type is None:
            raise forms.ValidationError("الصيغ المسموحة: PDF أو PNG أو JPG.")
        return {"name": upload.name[:255], "content_type": content_type, "data": data}


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        if not files:
            raise forms.ValidationError("اختر صورة واحدة على الأقل.")
        if len(files) > MAX_PHOTOS_PER_UPLOAD:
            raise forms.ValidationError(f"الحد الأقصى {MAX_PHOTOS_PER_UPLOAD} صورة في المرة الواحدة.")
        images, errors = [], []
        for upload in files:
            try:
                images.append(image_data_uri(upload))
            except forms.ValidationError as exc:
                errors.extend(exc.messages)
        if errors:
            raise forms.ValidationError(errors)
        return images


class AlbumPhotosForm(forms.Form):
    photos = MultipleImageField(label="الصور", help_text=f"حتى {MAX_PHOTOS_PER_UPLOAD} صورة، كل صورة 800 كيلوبايت كحد أقصى")
    caption = forms.CharField(label="وصف الصور (اختياري)", max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


# ---------------------------------------------------------------- نماذج الموقع العام

PHONE_RE = r"^(\+?966|0)?5\d{8}$"


class HoneypotMixin(forms.Form):
    website = forms.CharField(required=False, label="اترك هذا الحقل فارغاً",
                              widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))


def clean_phone(value):
    import re

    value = re.sub(r"[\s-]", "", value or "")
    if not re.fullmatch(PHONE_RE, value):
        raise forms.ValidationError("أدخل رقم جوال سعودي صحيحاً مثل 05xxxxxxxx.")
    return value


class ApplyForm(HoneypotMixin):
    """طلب مساعدة / تسجيل مستفيد من الموقع: يُنشئ ملف مستفيد بحالة «جديد» لدراسته."""

    full_name = forms.CharField(label="الاسم الرباعي", max_length=150)
    national_id = forms.CharField(label="رقم الهوية / الإقامة", max_length=10, min_length=10)
    phone = forms.CharField(label="رقم الجوال", max_length=20)
    nationality = forms.CharField(label="الجنسية", max_length=50, initial="سعودي")
    gender = forms.ChoiceField(label="الجنس")
    marital_status = forms.ChoiceField(label="الحالة الاجتماعية", required=False)
    city = forms.CharField(label="المدينة", max_length=60)
    district = forms.CharField(label="الحي", max_length=60, required=False)
    category = forms.ModelChoiceField(label="الفئة", queryset=None, required=False, empty_label="— اختر —")
    housing_type = forms.ChoiceField(label="نوع السكن", required=False)
    monthly_income = forms.DecimalField(label="دخل رب الأسرة الشهري (ريال)", min_value=0, max_digits=12,
                                        decimal_places=2, required=False)
    family_size = forms.IntegerField(label="عدد أفراد الأسرة", min_value=1, max_value=40, initial=1)
    needs = forms.CharField(label="نوع المساعدة المطلوبة وتفاصيل الحالة", max_length=2000,
                            widget=forms.Textarea(attrs={"rows": 4}))
    consent = forms.BooleanField(label="أقر بصحة البيانات وأوافق على تواصل الجمعية معي لدراسة الحالة")

    def __init__(self, *args, **kwargs):
        from beneficiaries.models import BeneficiaryCategory, Gender, HousingType, MaritalStatus

        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = BeneficiaryCategory.objects.all()
        self.fields["gender"].choices = Gender.choices
        self.fields["marital_status"].choices = [("", "— اختر —")] + list(MaritalStatus.choices)
        self.fields["housing_type"].choices = [("", "— اختر —")] + list(HousingType.choices)
        self.fields["national_id"].widget.attrs.update({"inputmode": "numeric", "dir": "ltr"})
        self.fields["phone"].widget.attrs.update({"inputmode": "tel", "dir": "ltr", "placeholder": "05xxxxxxxx"})

    def clean_national_id(self):
        from beneficiaries.models import validate_national_id

        value = self.cleaned_data["national_id"].strip()
        validate_national_id(value)
        return value

    def clean_phone(self):
        return clean_phone(self.cleaned_data["phone"])


class ContactForm(HoneypotMixin):
    name = forms.CharField(label="الاسم", max_length=120)
    phone = forms.CharField(label="رقم الجوال", max_length=20, required=False)
    email = forms.EmailField(label="البريد الإلكتروني", required=False)
    subject = forms.CharField(label="الموضوع", max_length=150)
    message = forms.CharField(label="الرسالة", max_length=3000, widget=forms.Textarea(attrs={"rows": 5}))

    def clean_phone(self):
        value = self.cleaned_data.get("phone")
        return clean_phone(value) if value else ""

    def clean(self):
        data = super().clean()
        if not data.get("phone") and not data.get("email") and not self.errors:
            raise forms.ValidationError("أدخل رقم الجوال أو البريد الإلكتروني لنتمكن من الرد عليك.")
        return data
