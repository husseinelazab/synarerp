from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "الإعدادات العامة"

    def ready(self):
        # تحميل تعريفات المستندات من كل الوحدات
        from importlib import import_module

        for app in ("core", "accounts", "stock", "selling", "buying", "hr", "pos", "assets", "manufacturing", "pricing", "crm", "projects", "support", "quality",
                    "beneficiaries", "funds", "governance", "awqaf", "volunteering", "office",
                    "programs", "excellence", "relations", "fleet", "website", "store"):
            import_module(f"{app}.doctypes")
        from core import audit  # noqa: F401 — تفعيل سجل التعديلات
        from core import security_log  # noqa: F401 — سجل أحداث الأمان

        # عزل الشركات داخل PostgreSQL: الدور المقيد لكل اتصال تشغيل، والسياسات بعد كل ترحيل
        from django.db.backends.signals import connection_created
        from django.db.models.signals import post_migrate

        from core import rls

        connection_created.connect(rls.on_connection_created, dispatch_uid="synar-rls-role")
        post_migrate.connect(_install_rls, sender=self, dispatch_uid="synar-rls-install")


def _install_rls(sender, using="default", **kwargs):
    from core import rls

    rls.install(using)
