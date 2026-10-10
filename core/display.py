from decimal import Decimal

from django.db import models

from core.utils import money

HIDDEN_FIELDS = {"id", "created_at", "modified_at", "parent", "company", "created_by", "docstatus",
                 # تظهر في بطاقة الإجماليات بدلاً من تفاصيل المستند
                 "base_net_total", "base_tax_amount", "base_grand_total", "base_amount",
                 # تظهر كشارة وشريط في رأس الصفحة
                 "status", "per_billed",
                 # حالة الموظف قبل المخالصة (لاستعادتها عند الإلغاء)
                 "previous_status", "previous_relieving_date"}


def display_value(obj, field_name):
    field = obj._meta.get_field(field_name)
    if field.choices:
        return getattr(obj, f"get_{field_name}_display")()
    value = getattr(obj, field_name)
    if value is None or value == "":
        return "—"
    if isinstance(field, models.BooleanField):
        return "نعم" if value else "لا"
    if isinstance(value, Decimal):
        return money(value) if field.decimal_places == 2 else f"{value.normalize():f}"
    return str(value)


def detail_fields(obj, exclude=()):
    rows = []
    for field in obj._meta.concrete_fields:
        if field.name in HIDDEN_FIELDS or field.name in exclude:
            continue
        value = getattr(obj, field.name)
        # حقول النظام (غير القابلة للتحرير) تُعرض فقط إن كانت ذات قيمة: مرتجع، وردية، مدفوع...
        if not field.editable and not field.choices and (value is None or value is False or value == 0 or value == ""):
            continue
        rows.append((field.verbose_name, display_value(obj, field.name)))
    return rows


def field_label(model, field_name):
    return model._meta.get_field(field_name).verbose_name
