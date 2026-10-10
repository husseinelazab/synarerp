from django.apps import apps
from django.contrib import admin

# تسجيل كل نماذج النظام في لوحة الإدارة تلقائياً
for app_label in ("core", "accounts", "stock", "selling", "buying", "hr", "pos", "saas"):
    for model in apps.get_app_config(app_label).get_models():
        if not admin.site.is_registered(model):
            admin.site.register(model)
