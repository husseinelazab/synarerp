from django import forms
from django.contrib.auth import get_user_model, password_validation

from core.forms import style_form
from core.models import Company, Role
from saas.models import PaymentMethod, Plan

User = get_user_model()


class SignupForm(forms.Form):
    company_name = forms.CharField(label="اسم المنشأة", max_length=150)
    tax_id = forms.CharField(label="الرقم الضريبي (اختياري)", max_length=20, required=False)
    phone = forms.CharField(label="رقم الجوال", max_length=30)
    full_name = forms.CharField(label="الاسم الكامل", max_length=150)
    email = forms.EmailField(label="البريد الإلكتروني")
    username = forms.CharField(label="اسم المستخدم", max_length=150,
                               help_text="بالأحرف الإنجليزية والأرقام، وتستخدمه لتسجيل الدخول")
    password = forms.CharField(label="كلمة المرور", widget=forms.PasswordInput)
    password2 = forms.CharField(label="تأكيد كلمة المرور", widget=forms.PasswordInput)
    plan = forms.ModelChoiceField(label="الباقة", queryset=Plan.objects.filter(is_public=True), empty_label=None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("اسم المستخدم مستخدم من قبل.")
        User.username_validator(username)
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("يوجد حساب مسجل بهذا البريد الإلكتروني.")
        return email

    def clean_company_name(self):
        name = self.cleaned_data["company_name"].strip()
        if Company.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError("يوجد منشأة مسجلة بهذا الاسم.")
        return name

    def clean(self):
        data = super().clean()
        if data.get("password") and data.get("password") != data.get("password2"):
            self.add_error("password2", "كلمتا المرور غير متطابقتين.")
        elif data.get("password"):
            try:
                password_validation.validate_password(data["password"])
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return data


class CompanySettingsForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)

    def clean_currency(self):
        from accounts.models import GLEntry

        currency = self.cleaned_data["currency"]
        if currency != self.instance.currency and GLEntry.all_objects.filter(company=self.instance).exists():
            raise forms.ValidationError("لا يمكن تغيير عملة الشركة بعد ترحيل قيود محاسبية.")
        return currency


class MemberCreateForm(forms.Form):
    full_name = forms.CharField(label="الاسم الكامل", max_length=150)
    username = forms.CharField(label="اسم المستخدم", max_length=150)
    email = forms.EmailField(label="البريد الإلكتروني", required=False)
    password = forms.CharField(label="كلمة المرور", widget=forms.PasswordInput)
    role = forms.ChoiceField(label="الدور", choices=[c for c in Role.choices if c[0] not in (Role.OWNER, Role.EMPLOYEE)])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("اسم المستخدم مستخدم من قبل.")
        User.username_validator(username)
        return username

    def clean_password(self):
        password_validation.validate_password(self.cleaned_data["password"])
        return self.cleaned_data["password"]


class MemberRoleForm(forms.Form):
    role = forms.ChoiceField(label="الدور", choices=[c for c in Role.choices if c[0] not in (Role.OWNER, Role.EMPLOYEE)])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


class RecordPaymentForm(forms.Form):
    plan = forms.ModelChoiceField(label="الباقة", queryset=Plan.objects.all(), empty_label=None)
    months = forms.IntegerField(label="عدد الأشهر", min_value=1, initial=12)
    amount = forms.DecimalField(label="المبلغ المدفوع", min_value=0, decimal_places=2)
    method = forms.ChoiceField(label="طريقة الدفع", choices=PaymentMethod.choices)
    reference = forms.CharField(label="المرجع (رقم الحوالة...)", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)
