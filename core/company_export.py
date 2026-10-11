"""تصدير كامل بيانات شركة واحدة في ملف ZIP.

- كل جدول يخص الشركة في ملف JSON مستقل (صيغة Django القياسية)، مع manifest.json بعدد السجلات.
- يشمل أسطر المستندات والجداول التابعة التي لا تحمل حقل الشركة مباشرة (عبر الحقل الذي يربطها بجدول للشركة).
- لا تُصدَّر الأسرار: كلمات المرور والرموز والمفاتيح (حتى المشفّرة منها)، ولا كلمات مرور المستخدمين.
- الجداول المشتركة بين كل الشركات (العملات، الباقات) لا تُصدَّر.
"""
import datetime
import io
import json
import zipfile

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import serializers
from django.utils import timezone

from core.audit import SENSITIVE

FORMAT_VERSION = 1
SKIP = {"core.usertwofactor", "core.currency", "saas.plan", "contenttypes.contenttype", "sessions.session",
        "admin.logentry", "auth.permission", "auth.group"}
USER_FIELDS = ("username", "first_name", "last_name", "email", "is_active", "date_joined", "last_login")

README = """تصدير كامل لبيانات الشركة «{name}» من نظام سِينار
تاريخ التصدير: {at}

- مجلد data: ملف JSON لكل جدول (اسم الملف = التطبيق.الجدول)، بصيغة Django القياسية (model, pk, fields).
- manifest.json: اسم كل جدول بالعربية وعدد سجلاته.
- الحقول المرتبطة تحمل رقم السجل المرتبط (pk) في الملف المناسب.
- لا يحتوي الملف على كلمات المرور أو الرموز السرية أو مفاتيح التشفير.

احفظ الملف في مكان آمن: يحتوي على كل بيانات شركتك (العملاء، الفواتير، الرواتب...).
"""


def _is_company_fk(field, company_model):
    return field.is_relation and field.many_to_one and field.related_model is company_model


def _exported_fields(model):
    return [f.name for f in model._meta.concrete_fields
            if not f.primary_key and not any(s in f.name for s in SENSITIVE)]


def company_querysets(company):
    """[(النموذج، الاستعلام)] لكل جدول يخص الشركة."""
    from core.models import Company

    direct, result = {}, []
    models = [m for m in apps.get_models() if m._meta.label_lower not in SKIP and m is not Company
              and m is not get_user_model()]
    for model in models:
        field = next((f for f in model._meta.concrete_fields if f.name == "company" and _is_company_fk(f, Company)),
                     None)
        if field is not None:
            direct[model] = model._base_manager.filter(company=company)
    for model in models:
        if model in direct:
            continue
        link = next((f for f in model._meta.concrete_fields
                     if f.is_relation and f.many_to_one and f.related_model in direct), None)
        if link is not None:
            result.append((model, model._base_manager.filter(**{f"{link.name}__company": company})))
    result = list(direct.items()) + result
    result.sort(key=lambda x: x[0]._meta.label_lower)
    return [(Company, Company.objects.filter(pk=company.pk))] + result


def _users(company):
    from core.models import Membership

    ids = Membership.objects.filter(company=company).values_list("user_id", flat=True)
    return get_user_model()._base_manager.filter(pk__in=ids)


def write_export(company, stream, user=None):
    """يكتب ملف ZIP في stream ويعيد قائمة الجداول وعدد سجلاتها."""
    serializer_class = serializers.get_serializer("json")
    manifest, now = [], timezone.localtime()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        sets = company_querysets(company) + [(get_user_model(), _users(company))]
        for model, qs in sets:
            count = qs.count()
            if not count:
                continue
            label = model._meta.label_lower
            fields = list(USER_FIELDS) if model is get_user_model() else _exported_fields(model)
            info = zipfile.ZipInfo(f"data/{label}.json", date_time=now.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            with zf.open(info, "w") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8")
                serializer_class().serialize(qs.order_by("pk").iterator(chunk_size=2000), stream=text,
                                             fields=fields, ensure_ascii=False)
                text.flush()
                text.detach()
            manifest.append({"table": label, "name": str(model._meta.verbose_name_plural), "count": count})
        zf.writestr("manifest.json", json.dumps({
            "format": "synar-company-export", "version": FORMAT_VERSION, "company": company.name,
            "company_id": company.pk, "exported_at": now.isoformat(),
            "exported_by": getattr(user, "username", "") or "", "tables": manifest,
        }, ensure_ascii=False, indent=2))
        zf.writestr("README.txt", README.format(name=company.name, at=now.strftime("%Y-%m-%d %H:%M")))
    return manifest


def export_filename(company):
    return f"synar-company-{company.pk}-{datetime.date.today():%Y%m%d}.zip"
