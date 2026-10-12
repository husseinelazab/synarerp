"""نسخ المستندات للتكرار، وتوليد المستحق منها يومياً (من المجدول)."""
from django.core.exceptions import ValidationError
from django.db import models

from core.models import SubmittableDocument

SKIP = {"id", "company", "name", "docstatus", "created_by", "created_at", "modified_at", "posting_date", "due_date",
        "remarks"}


def clone_document(source, posting_date):
    """نسخة مسودة من المستند وبنوده بتاريخ جديد (الحقول المحسوبة تُعاد حسابها)."""
    from core.registry import REGISTRY

    model = type(source)
    values = {}
    for field in model._meta.concrete_fields:
        if field.name in SKIP or not field.editable or isinstance(field, models.AutoField):
            continue
        values[field.attname] = getattr(source, field.attname)
    doc = model(company=source.company, posting_date=posting_date, created_by=source.created_by,
                remarks=source.remarks, **values)
    if getattr(source, "due_date", None):
        doc.due_date = posting_date + (source.due_date - source.posting_date)
    doc.save()
    child = REGISTRY[source.doctype_slug].child
    if child is not None:
        related = child.model._meta.get_field(child.fk_name).remote_field.related_name
        for row in getattr(source, related).all():
            if getattr(row, "is_free_item", False):
                continue  # الهدايا تُعاد من العروض السارية
            data = {f.attname: getattr(row, f.attname) for f in child.model._meta.concrete_fields
                    if f.editable and f.name not in ("id", child.fk_name)}
            getattr(doc, related).create(**data)
    doc.calculate_totals()
    doc.save()
    return doc


def make_recurring(source, frequency, first_date=None, end_date=None, submit_automatically=False, user=None):
    from core.models import RecurringDocument, add_period

    if not isinstance(source, SubmittableDocument) or not source.is_submitted:
        raise ValidationError("يتكرر المستند المعتمد فقط.")
    if getattr(source, "is_return", False) or getattr(source, "is_pos", False):
        raise ValidationError("لا تتكرر المرتجعات وفواتير نقاط البيع.")
    return RecurringDocument.objects.create(
        company=source.company, title=f"{source._meta.verbose_name} {source} — {getattr(source, 'party', '') or ''}".strip(" —"),
        doctype=source.doctype_slug, source_id=source.pk, frequency=frequency,
        next_date=first_date or add_period(source.posting_date, frequency), end_date=end_date,
        submit_automatically=submit_automatically)


def run_due(today):
    """يولّد كل المستندات المستحقة حتى اليوم لكل الشركات؛ تعيد [(القالب، المستند أو الخطأ)]."""
    from core.models import Company, RecurringDocument
    from core.notify import notify_managers
    from core.tenancy import tenant_context

    results = []
    for company in Company.objects.all():
        with tenant_context(company):
            for rec in RecurringDocument.objects.filter(disabled=False, next_date__lte=today):
                while not rec.disabled and rec.next_date <= today:
                    try:
                        doc = rec.generate()
                    except ValidationError as exc:
                        notify_managers(company, f"تعذر إنشاء المستند المتكرر «{rec}»: {' '.join(exc.messages)}",
                                        f"/app/recurring-document/{rec.pk}/")
                        results.append((rec, exc))
                        break
                    results.append((rec, doc))
                    if not doc.is_submitted:
                        notify_managers(company, f"أُنشئت مسودة {doc._meta.verbose_name} {doc} من «{rec}» — راجعها واعتمدها",
                                        doc.get_absolute_url())
    return results
