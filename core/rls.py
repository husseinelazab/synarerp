"""عزل بيانات الشركات داخل قاعدة البيانات نفسها (PostgreSQL Row Level Security).

طبقة حماية ثانية تحت ترشيح التطبيق (TenantManager): كل جدول لنموذج يرث TenantModel عليه سياسة تجعل
قاعدة البيانات لا تُظهر ولا تقبل إلا صفوف الشركة المضبوطة في متغير الجلسة ``synar.company_id``.
فلو نسي استعلامٌ الترشيحَ (all_objects، أو SQL مباشر، أو خطأ برمجي) لا تخرج بيانات شركة أخرى في طلبات
مستخدمي الشركات.

- يُضبط المتغير تلقائياً مع الشركة الحالية (core.tenancy): في كل طلب لمستخدم شركة وداخل tenant_context.
- بلا شركة مضبوطة (المهام الدورية، والصفحات العامة، ولوحة المنصة، والأوامر الإدارية) لا تقييد: هذه مسارات
  النظام التي تعمل على كل الشركات عمداً.
- مستخدم قاعدة البيانات في صورة Docker مدير (superuser) والمدير يتجاوز RLS دائماً، فيعمل الخادم والمجدول
  بدور مقيد ``synar_tenant`` (SET ROLE) لا يملك الجداول ولا تجاوز RLS، بينما تعمل الترحيلات بالمستخدم المالك.
"""
import logging
import os

from django.conf import settings
from django.db import DatabaseError, connections

log = logging.getLogger(__name__)

POLICY = "synar_tenant_isolation"
SETTING = "synar.company_id"
_CONDITION = (f"coalesce(current_setting('{SETTING}', true), '') = '' "
              f"OR company_id = nullif(current_setting('{SETTING}', true), '')::bigint")

# الخادم والمجدول يفعّلانه (deploy/entrypoint.sh)؛ الأوامر الإدارية والترحيلات تعمل بالمستخدم المالك
RUNTIME = os.environ.get("SYNAR_DB_RUNTIME_ROLE") == "1"


def role():
    return getattr(settings, "SYNAR_DB_RLS_ROLE", "synar_tenant")


def enabled(connection):
    return connection.vendor == "postgresql" and getattr(settings, "SYNAR_DB_RLS", True)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def tenant_tables():
    from django.apps import apps

    from core.models import TenantModel

    return sorted({m._meta.db_table for m in apps.get_models()
                   if issubclass(m, TenantModel) and m._meta.managed and not m._meta.proxy})


def install(using="default"):
    """ينشئ الدور المقيد وصلاحياته ويفعّل السياسة على كل جداول الشركات. يُشغَّل بعد كل ترحيل (يتكرر بأمان)."""
    connection = connections[using]
    if not enabled(connection):
        return []
    name = _quote(role())
    tables = tenant_tables()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role()])
        if cursor.fetchone() is None:
            cursor.execute(f"CREATE ROLE {name} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT")
        cursor.execute(f"GRANT {name} TO current_user")
        cursor.execute(f"GRANT USAGE ON SCHEMA public TO {name}")
        cursor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {name}")
        cursor.execute(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {name}")
        cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {name}")
        cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {name}")
        for table in tables:
            quoted = _quote(table)
            cursor.execute(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY")
            cursor.execute(f"DROP POLICY IF EXISTS {POLICY} ON {quoted}")
            cursor.execute(f"CREATE POLICY {POLICY} ON {quoted} USING ({_CONDITION}) WITH CHECK ({_CONDITION})")
    return tables


def activate_role(connection):
    """تشغيل الاتصال بالدور المقيد حتى تسري السياسات (المدير والمالك يتجاوزانها)."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {_quote(role())}")


def on_connection_created(sender, connection, **kwargs):
    if not (RUNTIME and enabled(connection)):
        return
    try:
        activate_role(connection)
    except DatabaseError:
        # الدور يُنشأ مع الترحيلات؛ دونه يعمل النظام بترشيح التطبيق وحده، ويُسجَّل الخطأ ليُعالج
        log.critical("تعذر تفعيل عزل الشركات في قاعدة البيانات (الدور %s غير موجود؟). شغّل migrate.", role())


def sync_company(company):
    """يضبط متغير الجلسة بالشركة الحالية (أو يفرغه). يُستدعى مع كل تغيير للشركة الحالية."""
    connection = connections["default"]
    if not enabled(connection):
        return
    value = str(company.pk) if company is not None and company.pk else ""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, false)", [SETTING, value])
    except DatabaseError:
        # معاملة متعثرة ستُلغى: يعود المتغير معها لقيمته قبلها، وهي قيمة السياق الخارجي
        pass


def status(using="default"):
    """للعرض والفحص: هل السياسات مفعّلة، وهل يعمل الاتصال بالدور المقيد."""
    connection = connections[using]
    if not enabled(connection):
        return {"available": False}
    tables = tenant_tables()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_policies WHERE policyname = %s AND tablename = ANY(%s)",
                       [POLICY, tables])
        protected = cursor.fetchone()[0]
        cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        user, superuser, bypass = cursor.fetchone()
    return {"available": True, "tables": len(tables), "protected": protected, "user": user,
            "enforced": protected == len(tables) and not superuser and not bypass}
