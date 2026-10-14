from django import forms
from django.contrib.auth.models import User

from core.forms import formfield_callback
from core.models import Company, TenantModel
from core.registry import ChildTable, DocType, register
from excellence.models import KPI, Initiative, KPIReading, StrategicGoal, StrategicPlan


class ScopedForm(forms.ModelForm):
    """حقول الاختيار تُبنى عند تحميل الوحدة، فنعيد ترشيحها بالشركة الحالية: سجلاتها ومستخدموها فقط."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = Company.current()
        for field in self.fields.values():
            if not isinstance(field, forms.ModelChoiceField):
                continue
            model = field.queryset.model
            if model is User:
                field.queryset = User.objects.filter(memberships__company=company,
                                                     memberships__is_active=True).distinct()
            elif issubclass(model, TenantModel):
                field.queryset = model.objects.all()


def scoped_form(model, fields):
    meta = type("Meta", (), {"model": model, "fields": fields, "formfield_callback": formfield_callback})
    return type(f"{model.__name__}Form", (ScopedForm,), {"Meta": meta})


register(DocType(
    slug="strategic-plan", model=StrategicPlan, module="excellence",
    list_fields=["title", "start_year", "end_year", "status"],
    search_fields=["title", "vision", "mission"], detail_template="excellence/_plan_detail.html",
))
register(DocType(
    slug="strategic-goal", model=StrategicGoal, module="excellence",
    form_class=scoped_form(StrategicGoal, ["plan", "code", "title", "description", "weight", "vision2030_pillar",
                                           "sdg", "owner"]),
    list_fields=["code", "title", "plan", "weight", "vision2030_pillar", "sdg", "owner"],
    search_fields=["code", "title", "description"], detail_template="excellence/_goal_detail.html",
))
register(DocType(
    slug="initiative", model=Initiative, module="excellence",
    form_class=scoped_form(Initiative, ["goal", "title", "owner", "department", "start_date", "end_date", "budget",
                                        "actual_cost", "progress", "weight", "status", "notes"]),
    list_fields=["title", "goal", "owner", "end_date", "progress", "status"],
    search_fields=["title", "department", "goal__title", "goal__code"],
))
register(DocType(
    slug="kpi", model=KPI, module="excellence",
    form_class=scoped_form(KPI, ["goal", "name", "unit", "baseline", "target", "direction", "frequency", "owner",
                                 "data_source", "formula"]),
    list_fields=["name", "goal", "unit", "target", "frequency", "owner"],
    search_fields=["name", "goal__title", "goal__code", "data_source"],
    child=ChildTable(KPIReading, ["period_end", "value", "note"], label="القراءات", fk_name="kpi", min_rows=0,
                     help="سجل قيمة المؤشر في نهاية كل فترة قياس، أو أدخل قراءات كل المؤشرات دفعة واحدة من «إدخال قراءات المؤشرات»."),
    detail_template="excellence/_kpi_detail.html",
))
