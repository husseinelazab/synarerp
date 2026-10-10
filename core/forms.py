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
    return field


def style_form(form):
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check")
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
