from django import forms
from django.db import models
from django.forms import inlineformset_factory, modelform_factory


def formfield_callback(db_field, **kwargs):
    if isinstance(db_field, models.DateField):
        kwargs["widget"] = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
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
        fields=child.fields,
        extra=0,
        min_num=1,
        can_delete=True,
        formfield_callback=formfield_callback,
    )
