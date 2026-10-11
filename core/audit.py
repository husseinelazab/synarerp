"""سجل التعديلات التلقائي عبر إشارات الحفظ والحذف.

- يُسجَّل الإنشاء والحذف، والتعديل بالقيم قبل وبعد للحقول التي يدخلها المستخدم (لا الحقول المحسوبة كالإجماليات).
- تغيّر حالة المستند يُسجَّل «اعتماد» أو «إلغاء».
- أسطر المستندات (البنود) تُسجَّل مرتبطة بمستندها.
- كلمات المرور والرموز والمفاتيح تُخفى.
- السجلات الآلية الكثيفة (قيود الأستاذ، حركات المخزون، الأرصدة، التنبيهات…) لا تُسجَّل لأنها ناتجة عن مستندات مسجلة.
"""
import datetime
import decimal
import logging

from django.apps import apps as global_apps
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

log = logging.getLogger(__name__)

TRACKED_APPS = {"core", "accounts", "stock", "selling", "buying", "hr", "pos", "assets", "manufacturing",
                "messaging", "einvoice", "saas"}
EXCLUDED = {
    "core.auditlog", "core.notification", "core.dataimport", "core.usertwofactor", "core.namingseries",
    "accounts.glentry", "stock.stockledgerentry", "stock.bin", "stock.batchbin", "stock.itempricehistory",
    "stock.stockcountscan", "messaging.messagelog", "hr.attendancepunch", "einvoice.einvoice",
    "pos.cardtransaction",
}
SENSITIVE = ("password", "secret", "token", "key_hash", "encrypted", "private_key", "csr")
IGNORED_FIELDS = {"id", "company", "created_by", "created_at", "modified_at", "updated_at"}


def tracked(model):
    meta = model._meta
    return (meta.app_label in TRACKED_APPS and meta.label_lower not in EXCLUDED and not meta.abstract
            and meta.apps is global_apps)


def audit_fields(model):
    cached = getattr(model, "_audit_fields", None)
    if cached is None:
        cached = [f for f in model._meta.concrete_fields
                  if f.name not in IGNORED_FIELDS and (f.editable or f.name == "docstatus") and not f.primary_key]
        model._audit_fields = cached
    return cached


def _value(field, value):
    if any(s in field.name for s in SENSITIVE):
        return "***" if value else ""
    if value is None:
        return None
    if isinstance(value, (decimal.Decimal, float)):
        return f"{value.normalize():f}" if isinstance(value, decimal.Decimal) else str(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bool, int, str)):
        return value if not isinstance(value, str) else value[:300]
    return str(value)[:300]


def _norm(field, value):
    """القيمة بنوع الحقل (1 و Decimal("1.00") و "1" قيمة واحدة)."""
    if value is None or field.is_relation:
        return value
    try:
        return field.to_python(value)
    except Exception:  # noqa: BLE001
        return value


def _display(instance, field, raw):
    """القيمة المقروءة: اسم السجل المرتبط أو تسمية الاختيار."""
    if raw is None:
        return None
    if field.is_relation:
        try:
            obj = field.related_model._base_manager.filter(pk=raw).first()
            return str(obj)[:200] if obj is not None else str(raw)
        except Exception:  # noqa: BLE001
            return str(raw)
    if field.choices:
        return str(dict(field.flatchoices).get(raw, raw))
    return _value(field, raw)


def _snapshot(instance):
    return {f.attname: getattr(instance, f.attname) for f in audit_fields(type(instance))}


def _parent(instance):
    for name in ("parent",):
        field = next((f for f in instance._meta.concrete_fields if f.name == name and f.is_relation), None)
        if field is not None:
            pid = getattr(instance, field.attname)
            if pid:
                return field.related_model, pid
    return None, None


def _company_id(instance, parent_model, parent_id):
    cid = getattr(instance, "company_id", None)
    if cid is None and parent_model is not None:
        cid = parent_model._base_manager.filter(pk=parent_id).values_list("company_id", flat=True).first() \
            if any(f.name == "company" for f in parent_model._meta.concrete_fields) else None
    return cid


def _write(instance, action, changes):
    from core.models import AuditLog
    from core.tenancy import get_current_ip, get_current_user

    parent_model, parent_id = _parent(instance)
    user = get_current_user()
    try:
        AuditLog.objects.create(
            company_id=_company_id(instance, parent_model, parent_id),
            user=user if getattr(user, "is_authenticated", False) else None,
            action=action, model_label=instance._meta.label_lower, model_name=str(instance._meta.verbose_name),
            object_id=str(instance.pk), object_repr=str(instance)[:200],
            parent_label=parent_model._meta.label_lower if parent_model else "", parent_id=str(parent_id or ""),
            changes=changes, ip=get_current_ip())
    except Exception:  # noqa: BLE001 — السجل لا يُفشل العملية الأصلية
        log.exception("تعذر تسجيل التعديل")


@receiver(pre_save)
def _before_save(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not tracked(sender) or instance._state.adding or not instance.pk:
        return
    fields = audit_fields(sender)
    if update_fields is not None and not {f.name for f in fields} & set(update_fields):
        return  # حفظ لحقول محسوبة فقط
    old = sender._base_manager.filter(pk=instance.pk).values(*[f.attname for f in fields]).first()
    instance._audit_old = old


@receiver(post_save)
def _after_save(sender, instance, created, raw=False, **kwargs):
    if raw or not tracked(sender):
        return
    if created:
        _write(instance, "create", {})
        return
    old = instance.__dict__.pop("_audit_old", None)
    if old is None:
        return
    changes, action = {}, "update"
    for field in audit_fields(sender):
        before, after = _norm(field, old.get(field.attname)), _norm(field, getattr(instance, field.attname))
        if field.name == "docstatus":
            if before != after:
                action = {1: "submit", 2: "cancel"}.get(after, "update")
            continue
        if _value(field, before) != _value(field, after):
            changes[str(field.verbose_name)] = [_display(instance, field, before), _display(instance, field, after)]
    if changes or action != "update":
        _write(instance, action, changes)


@receiver(post_delete)
def _after_delete(sender, instance, **kwargs):
    if not tracked(sender):
        return
    snapshot = {str(f.verbose_name): _display(instance, f, getattr(instance, f.attname))
                for f in audit_fields(sender) if getattr(instance, f.attname) not in (None, "", False)}
    _write(instance, "delete", snapshot)
