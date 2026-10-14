from django.apps import AppConfig


class GrantsConfig(AppConfig):
    name = "grants"
    verbose_name = "إدارة المنح"

    def ready(self):
        from grants import doctypes  # noqa: F401 — تسجيل مستندات المنح
