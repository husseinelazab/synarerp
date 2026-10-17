import datetime
from decimal import Decimal

from django.db import models
from django.utils import timezone

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
    if field_name == "doctype" and hasattr(obj, "doctype_label"):
        return obj.doctype_label
    value = getattr(obj, field_name)
    if field.many_to_many:
        return "، ".join(str(row) for row in value.all()) or "—"
    if value is None or value == "":
        return "—"
    if isinstance(field, models.BooleanField):
        return "نعم" if value else "لا"
    if isinstance(value, Decimal):
        return money(value) if field.decimal_places == 2 else f"{value.normalize():f}"
    if isinstance(value, datetime.datetime):  # بتوقيت الرياض لا UTC
        return (timezone.localtime(value) if timezone.is_aware(value) else value).strftime("%Y-%m-%d %H:%M")
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
