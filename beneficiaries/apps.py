from django.apps import AppConfig


class BeneficiariesConfig(AppConfig):
    name = "beneficiaries"
    verbose_name = "المستفيدون والجهات المستفيدة"

    def ready(self):
        from beneficiaries import signals  # noqa: F401 — تجهيز بيانات الجهة غير الربحية
