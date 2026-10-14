from django.apps import AppConfig


class PortalConfig(AppConfig):
    name = "portal"
    verbose_name = "بوابة المستفيد"

    def ready(self):
        from portal import doctypes  # noqa: F401 — تسجيل مستندات البوابة في سجل أنواع المستندات
