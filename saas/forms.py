from django import forms
from django.contrib.auth import get_user_model, password_validation

from core.forms import style_form
from core.models import Company, OrganizationType, Role
from saas.models import PaymentMethod, Plan

User = get_user_model()


class SignupForm(forms.Form):
    company_name = forms.CharField(label="اسم المنشأة", max_length=150)
    organization_type = forms.ChoiceField(label="نوع المنشأة", choices=OrganizationType.choices,
                                          initial=OrganizationType.BUSINESS, required=False)
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

    def clean_organization_type(self):
        return self.cleaned_data["organization_type"] or OrganizationType.BUSINESS

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

    hidden_modules = forms.MultipleChoiceField(
        label="إخفاء وحدات لا تستخدمها", required=False, widget=forms.CheckboxSelectMultiple,
        help_text="تختفي من القائمة الجانبية لكل المستخدمين. لا تُحذف بياناتها، وتعود بإلغاء التحديد.")

    NONPROFIT_FIELDS = ("license_number", "registration_date", "headquarters", "objectives", "fs_policies",
                        "eligibility_threshold", "review_period_months", "require_aid_otp")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.is_nonprofit:
            for name in self.NONPROFIT_FIELDS:
                self.fields.pop(name)
        from core.permissions import hideable_modules

        self.fields["hidden_modules"].choices = hideable_modules(self.instance)
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
    modules = forms.MultipleChoiceField(
        label="الوحدات المسموحة", required=False, widget=forms.CheckboxSelectMultiple,
        help_text="للدور «مستخدم» فقط. لا تحدد شيئاً للسماح بكل الوحدات.")
    can_submit = forms.BooleanField(label="يعتمد المستندات (وإلا يُنشئ مسودات فقط)", required=False)
    can_cancel = forms.BooleanField(label="يلغي المستندات المعتمدة", required=False)
    can_delete = forms.BooleanField(label="يحذف المسودات والسجلات", required=False)
    can_export = forms.BooleanField(label="يصدّر القوائم إلى Excel", required=False)
    hide_costs = forms.BooleanField(label="إخفاء التكاليف والأرباح (أسعار الشراء والتقييم وتقارير الربحية)", required=False)
    denied_doctypes = forms.MultipleChoiceField(label="مستندات محجوبة تماماً", required=False,
                                                widget=forms.CheckboxSelectMultiple)
    read_only_doctypes = forms.MultipleChoiceField(label="مستندات للاطلاع فقط (بلا إنشاء أو تعديل)", required=False,
                                                   widget=forms.CheckboxSelectMultiple)
    cost_centers = forms.ModelMultipleChoiceField(
        label="الفروع / مراكز التكلفة المسموحة", required=False, queryset=None,
        widget=forms.CheckboxSelectMultiple,
        help_text="يرى المستخدم مستندات هذه الفروع فقط ويُنشئ عليها. لا تحدد شيئاً للسماح بكل الفروع.")

    def __init__(self, *args, **kwargs):
        from accounts.models import CostCenter
        from core.permissions import company_modules

        super().__init__(*args, **kwargs)
        modules = company_modules(Company.current())
        self.fields["modules"].choices = modules
        from core.permissions import PERMISSION_MODULES
        from core.registry import MODULES, REGISTRY

        labels = {**MODULES, **dict(modules)}
        hidden = {key for key, _ in PERMISSION_MODULES} - set(dict(modules))
        doctypes = sorted(((slug, f"{labels.get(dt.module, '')}: {dt.label_plural}") for slug, dt in REGISTRY.items()
                           if dt.module != "setup" and dt.module not in hidden), key=lambda c: c[1])
        self.fields["denied_doctypes"].choices = doctypes
        self.fields["read_only_doctypes"].choices = doctypes
        self.fields["cost_centers"].queryset = CostCenter.objects.filter(is_group=False, disabled=False)
        style_form(self)
        for name in ("modules", "cost_centers", "denied_doctypes", "read_only_doctypes"):
            self.fields[name].widget.attrs.pop("class", None)


class RecordPaymentForm(forms.Form):
    plan = forms.ModelChoiceField(label="الباقة", queryset=Plan.objects.all(), empty_label=None)
    months = forms.IntegerField(label="عدد الأشهر", min_value=1, initial=12)
    amount = forms.DecimalField(label="المبلغ المدفوع", min_value=0, decimal_places=2)
    method = forms.ChoiceField(label="طريقة الدفع", choices=PaymentMethod.choices)
    reference = forms.CharField(label="المرجع (رقم الحوالة...)", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


class PlanForm(forms.ModelForm):
    modules = forms.MultipleChoiceField(
        label="الوحدات المفتوحة في الباقة", required=False, widget=forms.CheckboxSelectMultiple,
        help_text="ما لا تحدده يُغلق ويختفي من قائمة شركات هذه الباقة. المحاسبة متاحة دائماً لأنها أساس النظام.")

    class Meta:
        model = Plan
        fields = ["name", "code", "description", "price_monthly", "price_yearly", "max_users", "trial_days",
                  "modules", "includes_ai", "is_public", "sort_order"]

    def __init__(self, *args, **kwargs):
        from core.permissions import PLAN_MODULES

        super().__init__(*args, **kwargs)
        self.fields["modules"].choices = PLAN_MODULES
        self.fields["modules"].initial = [key for key, _ in PLAN_MODULES
                                          if self.instance.pk is None or self.instance.allows_module(key)]
        style_form(self)
        self.fields["modules"].widget.attrs.pop("class", None)

    def save(self, commit=True):
        from core.permissions import PLAN_MODULES

        opened = set(self.cleaned_data["modules"])
        self.instance.includes_pos = "pos" in opened
        self.instance.closed_modules = [key for key, _ in PLAN_MODULES if key != "pos" and key not in opened]
        return super().save(commit)
