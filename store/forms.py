import base64

from django import forms

from core.forms import PrintSettingsForm, formfield_callback, style_form
from store.models import DonationOpportunity, Marketer, StoreSettings
from store.service import normalize_phone

MAX_IMAGE = 800 * 1024


def image_data_uri(upload):
    if upload.size > MAX_IMAGE:
        raise forms.ValidationError("حجم الصورة أكبر من 800 كيلوبايت.")
    data = upload.read()
    kind = next((t for magic, t in PrintSettingsForm.LOGO_TYPES.items() if data.startswith(magic)), None)
    if kind is None:
        raise forms.ValidationError("الصيغ المسموحة: PNG أو JPG أو WebP.")
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


class OpportunityForm(forms.ModelForm):
    class Meta:
        model = DonationOpportunity
        fields = ["title", "kind", "description", "target_amount", "fixed_amount", "allow_custom_amount", "fund",
                  "waqf", "sponsorship_type", "project", "is_published", "is_featured", "sort_order", "end_date"]
        formfield_callback = formfield_callback
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class OpportunityImageForm(forms.Form):
    image = forms.FileField(label="صورة الفرصة", required=False,
                            help_text="PNG أو JPG أو WebP بحجم 800 كيلوبايت كحد أقصى، يُفضّل بنسبة 16:9")
    remove = forms.BooleanField(label="حذف الصورة الحالية", required=False)

    def clean_image(self):
        upload = self.cleaned_data.get("image")
        return image_data_uri(upload) if upload else None


class MarketerForm(forms.ModelForm):
    class Meta:
        model = Marketer
        fields = ["name", "code", "phone", "is_active"]


class StoreSettingsForm(forms.ModelForm):
    secret_key = forms.CharField(label="المفتاح السري (Secret Key)", required=False,
                                 widget=forms.PasswordInput(render_value=False),
                                 help_text="يبدأ بـ sk_test_ أو sk_live_. اتركه فارغاً للإبقاء على المحفوظ")
    webhook_secret = forms.CharField(label="رمز الويب هوك السري (Secret Token)", required=False,
                                     widget=forms.PasswordInput(render_value=False),
                                     help_text="نفس الرمز الذي تكتبه عند إضافة الويب هوك في لوحة ميسّر. "
                                               "اتركه فارغاً للإبقاء على المحفوظ")

    apple_pay_file = forms.FileField(
        label="ملف توثيق Apple Pay", required=False,
        help_text="الملف apple-developer-merchantid-domain-association من لوحة ميسّر (Apple Pay ← إضافة نطاق)")
    remove_apple_pay_file = forms.BooleanField(label="حذف ملف توثيق Apple Pay المحفوظ", required=False)

    APPLE_PAY_MAX = 32 * 1024

    class Meta:
        model = StoreSettings
        fields = ["enabled", "mode", "publishable_key", "secret_key", "webhook_secret", "clearing_account",
                  "suggested_amounts", "thank_you_message", "allow_recurring", "gold_gram_price"]
        widgets = {"thank_you_message": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["clearing_account"].required = True
        if not self.instance.apple_pay_domain_file:
            del self.fields["remove_apple_pay_file"]
        style_form(self)

    def clean_apple_pay_file(self):
        upload = self.cleaned_data.get("apple_pay_file")
        if not upload:
            return ""
        if upload.size > self.APPLE_PAY_MAX:
            raise forms.ValidationError("حجم الملف أكبر من المتوقع لملف توثيق Apple Pay.")
        try:
            text = upload.read().decode("ascii").strip()
        except UnicodeDecodeError:
            raise forms.ValidationError("هذا ليس ملف توثيق Apple Pay (الملف الصحيح نص بلا امتداد من لوحة ميسّر).")
        # الملف سلسلة رموز ست عشرية (وقد يأتي بصيغة JSON)، وليس صفحة ولا نصاً برمجياً
        if not text or "<" in text or not all(32 <= ord(c) < 127 or c in "\r\n\t" for c in text):
            raise forms.ValidationError("محتوى الملف غير صالح لتوثيق Apple Pay.")
        return text

    def clean(self):
        data = super().clean()
        mode = data.get("mode")
        pk = (data.get("publishable_key") or "").strip()
        sk = (data.get("secret_key") or "").strip()
        expected = f"_{mode}_"
        if pk and (not pk.startswith("pk_") or expected not in pk):
            self.add_error("publishable_key", f"المفتاح العام يبدأ بـ pk{expected} في الوضع "
                                              f"«{dict(self.fields['mode'].choices).get(mode)}».")
        if sk and (not sk.startswith("sk_") or expected not in sk):
            self.add_error("secret_key", f"المفتاح السري يبدأ بـ sk{expected} في هذا الوضع.")
        if data.get("enabled") and not (sk or self.instance.secret_key_encrypted):
            self.add_error("secret_key", "أدخل المفتاح السري قبل تفعيل المتجر.")
        if data.get("enabled") and not pk:
            self.add_error("publishable_key", "أدخل المفتاح العام قبل تفعيل المتجر.")
        data["publishable_key"] = pk
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("secret_key"):
            obj.secret_key = self.cleaned_data["secret_key"].strip()
        if self.cleaned_data.get("webhook_secret"):
            obj.webhook_secret = self.cleaned_data["webhook_secret"].strip()
        if self.cleaned_data.get("apple_pay_file"):
            obj.apple_pay_domain_file = self.cleaned_data["apple_pay_file"]
        elif self.cleaned_data.get("remove_apple_pay_file"):
            obj.apple_pay_domain_file = ""
        if commit:
            obj.save()
        return obj


class HoneypotForm(forms.Form):
    website = forms.CharField(required=False, widget=forms.HiddenInput)  # مصيدة للبرامج الآلية


class CheckoutForm(HoneypotForm):
    donor_name = forms.CharField(label="الاسم", max_length=150, required=False)
    donor_phone = forms.CharField(label="رقم الجوال", max_length=20,
                                  help_text="يصلك عليه إشعار التبرع وشهادة الشكر")
    donor_email = forms.EmailField(label="البريد الإلكتروني (اختياري)", required=False)
    is_anonymous = forms.BooleanField(label="تبرع باسم «فاعل خير»", required=False)
    gift_to_name = forms.CharField(label="إهداء التبرع إلى (اختياري)", max_length=150, required=False)
    gift_to_phone = forms.CharField(label="جوال المُهدى إليه", max_length=20, required=False)
    gift_message = forms.CharField(label="رسالة الإهداء", max_length=300, required=False,
                                   widget=forms.Textarea(attrs={"rows": 2}))
    is_monthly = forms.BooleanField(label="تبرع شهري (يُخصم المبلغ نفسه كل شهر ويمكنك إيقافه من حسابك)",
                                    required=False)

    def __init__(self, *args, allow_monthly=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_monthly:
            del self.fields["is_monthly"]

    def clean_donor_phone(self):
        phone = normalize_phone(self.cleaned_data["donor_phone"])
        if not phone:
            raise forms.ValidationError("أدخل رقم جوال صحيحاً.")
        return phone

    def clean_gift_to_phone(self):
        raw = self.cleaned_data.get("gift_to_phone")
        if not raw:
            return ""
        phone = normalize_phone(raw)
        if not phone:
            raise forms.ValidationError("أدخل رقم جوال صحيحاً.")
        return phone

    def clean(self):
        data = super().clean()
        if data.get("is_anonymous"):
            data["donor_name"] = (data.get("donor_name") or "").strip() or "فاعل خير"
        elif not (data.get("donor_name") or "").strip():
            self.add_error("donor_name", "أدخل اسمك أو اختر «فاعل خير».")
        if data.get("gift_to_phone") and not data.get("gift_to_name"):
            self.add_error("gift_to_name", "اكتب اسم المُهدى إليه.")
        return data


class PhoneForm(HoneypotForm):
    phone = forms.CharField(label="رقم الجوال", max_length=20)

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        if not phone:
            raise forms.ValidationError("أدخل رقم جوال صحيحاً.")
        return phone


class CodeForm(HoneypotForm):
    code = forms.RegexField(label="رمز التحقق", regex=r"^\d{6}$", max_length=6,
                            error_messages={"invalid": "الرمز من 6 أرقام."})
