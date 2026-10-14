from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    name = "campaigns"
    verbose_name = "حملات التواصل مع الداعمين"

    def ready(self):
        from campaigns import doctypes  # noqa: F401 — تسجيل أنواع المستندات
