from django import forms
from core.forms import PrintSettingsForm
from office.models import Correspondence


class CorrespondenceForm(forms.ModelForm):
    signature_file = forms.FileField(label="رفع توقيع الموظف المُصدر", required=False,
        help_text="صورة PNG أو JPG أو WebP حتى 500 كيلوبايت.")
    remove_signature = forms.BooleanField(label="إزالة التوقيع", required=False)

    class Meta:
        model = Correspondence
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import Company
        from django.contrib.auth import get_user_model
        company = Company.current()
        self.fields["signatory"].queryset = get_user_model().objects.filter(memberships__company=company, memberships__is_active=True).distinct()

    def clean_signature_file(self):
        return PrintSettingsForm._clean_brand_image(self, self.cleaned_data.get("signature_file"))

    MAX_LOGO = PrintSettingsForm.MAX_LOGO
    LOGO_TYPES = PrintSettingsForm.LOGO_TYPES

    def save(self, commit=True):
        row = super().save(commit=False)
        if self.cleaned_data.get("remove_signature"):
            row.signature = ""
        if self.cleaned_data.get("signature_file"):
            row.signature = self.cleaned_data["signature_file"]
        if commit:
            row.save()
            self.save_m2m()
        return row
