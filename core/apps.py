from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "الإعدادات العامة"

    def ready(self):
        # تحميل تعريفات المستندات من كل الوحدات
        from importlib import import_module

        for app in ("core", "accounts", "stock", "selling", "buying", "hr", "pos", "assets"):
            import_module(f"{app}.doctypes")
