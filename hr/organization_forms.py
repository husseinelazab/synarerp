from django import forms
from core.models import Company
from core.permissions import company_modules
from core.registry import REGISTRY
from hr.models import AssociationRole


class AssociationRoleForm(forms.ModelForm):
    modules = forms.MultipleChoiceField(label="الوحدات المسموحة (فارغة = كل المتاح للجمعية)", required=False, widget=forms.CheckboxSelectMultiple)
    denied_doctypes = forms.MultipleChoiceField(label="مستندات محجوبة", required=False, widget=forms.CheckboxSelectMultiple)
    read_only_doctypes = forms.MultipleChoiceField(label="مستندات للاطلاع فقط", required=False, widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = AssociationRole
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        modules = company_modules(Company.current())
        self.fields["modules"].choices = modules
        allowed = {k for k, label in modules}
        choices = sorted((slug, str(dt.label_plural)) for slug, dt in REGISTRY.items() if dt.module in allowed)
        self.fields["denied_doctypes"].choices = choices
        self.fields["read_only_doctypes"].choices = choices
