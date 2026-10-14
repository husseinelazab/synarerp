"""نماذج بوابة المستفيد."""
import os
import re

from django import forms

from beneficiaries.models import HousingType
from portal.models import ServiceKind

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 10
# البصمة الأولى للملف حسب نوعه: لا نثق بالامتداد أو نوع المحتوى الذي يرسله المتصفح
SIGNATURES = {
    "pdf": (b"%PDF-", "application/pdf"),
    "png": (b"\x89PNG\r\n\x1a\n", "image/png"),
    "jpg": (b"\xff\xd8\xff", "image/jpeg"),
    "jpeg": (b"\xff\xd8\xff", "image/jpeg"),
}
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _ascii_digits(value):
    return re.sub(r"[\s-]", "", (value or "").translate(_DIGITS))


class HoneypotForm(forms.Form):
    website = forms.CharField(required=False, widget=forms.HiddenInput)  # مصيدة للبرامج الآلية


class LoginForm(HoneypotForm):
    national_id = forms.CharField(label="رقم الهوية / الإقامة", max_length=14)
    phone = forms.CharField(label="رقم الجوال المسجل لدى الجمعية", max_length=20)

    def clean_national_id(self):
        value = _ascii_digits(self.cleaned_data["national_id"])
        if not re.fullmatch(r"[12]\d{9}", value):
            raise forms.ValidationError("رقم الهوية أو الإقامة 10 أرقام ويبدأ بـ1 أو 2.")
        return value

    def clean_phone(self):
        from messaging.service import normalize_phone

        phone = normalize_phone(_ascii_digits(self.cleaned_data["phone"]))
        if not phone:
            raise forms.ValidationError("أدخل رقم جوال صحيحاً.")
        return phone


class CodeForm(HoneypotForm):
    code = forms.CharField(label="رمز التحقق", max_length=6)

    def clean_code(self):
        value = _ascii_digits(self.cleaned_data["code"])
        if not re.fullmatch(r"\d{6}", value):
            raise forms.ValidationError("الرمز من 6 أرقام.")
        return value


class ProfileForm(HoneypotForm):
    phone = forms.CharField(label="رقم الجوال", max_length=20)
    city = forms.CharField(label="المدينة", max_length=60, required=False)
    district = forms.CharField(label="الحي", max_length=60, required=False)
    housing_type = forms.ChoiceField(label="نوع السكن", required=False,
                                     choices=[("", "— اختر —")] + list(HousingType.choices))
    note = forms.CharField(label="ملاحظة (اختياري)", max_length=1000, required=False,
                           widget=forms.Textarea(attrs={"rows": 3}))

    def clean_phone(self):
        value = _ascii_digits(self.cleaned_data["phone"])
        if not re.fullmatch(r"(05\d{8}|\+?9665\d{8})", value):
            raise forms.ValidationError("أدخل رقم جوال سعودي صحيحاً مثل 05xxxxxxxx.")
        return value


class ServiceRequestForm(HoneypotForm):
    kind = forms.ChoiceField(label="نوع الطلب", choices=ServiceKind.choices)
    description = forms.CharField(label="تفاصيل الطلب", min_length=10, max_length=2000,
                                  widget=forms.Textarea(attrs={"rows": 4}))


def check_upload(f):
    """يعيد (نوع المحتوى، الخطأ) بعد التحقق من الامتداد والحجم وبصمة الملف الفعلية."""
    ext = os.path.splitext(f.name or "")[1].lower().lstrip(".")
    if ext not in SIGNATURES:
        return "", f"«{f.name}»: الأنواع المسموحة PDF و JPG و PNG فقط."
    if f.size > MAX_FILE_BYTES:
        return "", f"«{f.name}»: حجم الملف يتجاوز 5 ميجابايت."
    if not f.size:
        return "", f"«{f.name}»: الملف فارغ."
    magic, content_type = SIGNATURES[ext]
    f.seek(0)
    head = f.read(len(magic))
    f.seek(0)
    if head != magic:
        return "", f"«{f.name}»: محتوى الملف لا يطابق نوعه."
    return content_type, ""
