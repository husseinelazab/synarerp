from core.models import FiscalYear
from core.registry import DocType, register

# الشركة نفسها تُدار من صفحة «إعدادات الشركة»، والعملات تديرها إدارة المنصة
register(DocType(
    slug="fiscal-year", model=FiscalYear, module="setup",
    list_fields=["name", "start_date", "end_date", "is_closed"], search_fields=["name"],
))

from core.models import ExchangeRate  # noqa: E402

register(DocType(
    slug="exchange-rate", model=ExchangeRate, module="setup",
    list_fields=["currency", "date", "rate"], search_fields=["currency__code", "currency__name"],
))

from django import forms  # noqa: E402

from core.forms import formfield_callback  # noqa: E402
from core.models import ApprovalRule, RecurringDocument  # noqa: E402


class ApprovalRuleForm(forms.ModelForm):
    doctype = forms.ChoiceField(label="نوع المستند")

    class Meta:
        model = ApprovalRule
        fields = ["doctype", "level", "min_amount", "approver_role", "approver", "disabled"]
        formfield_callback = formfield_callback

    def __init__(self, *args, **kwargs):
        from django.contrib.auth.models import User

        from core.models import Company
        from core.registry import REGISTRY

        super().__init__(*args, **kwargs)
        self.fields["doctype"].choices = sorted(((slug, str(dt.label)) for slug, dt in REGISTRY.items()
                                                 if dt.is_submittable), key=lambda c: c[1])
        company = Company.current()
        self.fields["approver"].queryset = User.objects.filter(memberships__company=company,
                                                               memberships__is_active=True).distinct()


register(DocType(
    slug="approval-rule", model=ApprovalRule, module="setup", form_class=ApprovalRuleForm,
    list_fields=["doctype", "level", "min_amount", "approver_role", "approver", "disabled"],
    search_fields=["doctype"], show_in_menu=False,
))

register(DocType(
    slug="recurring-document", model=RecurringDocument, module="setup",
    list_fields=["title", "doctype", "frequency", "next_date", "end_date", "generated_count", "last_generated", "disabled"],
    form_fields=["title", "frequency", "next_date", "end_date", "submit_automatically", "disabled"],
    search_fields=["title"], show_in_menu=False,
))
