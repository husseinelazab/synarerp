from django.apps import AppConfig


class MessagingConfig(AppConfig):
    name = "messaging"
    verbose_name = "الرسائل (SMS وواتساب)"

    def ready(self):
        from messaging import signals  # noqa: F401 — ربط الإرسال الآلي عند اعتماد المستندات
