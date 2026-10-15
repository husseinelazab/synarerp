from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Company


@receiver(post_save, sender=Company)
def prepare_nonprofit(sender, instance, **kwargs):
    if instance.is_nonprofit:
        from beneficiaries.setup import ensure_nonprofit_setup
        from core.tenancy import tenant_context

        # بسياق الجمعية نفسها: عزل قاعدة البيانات لا يقبل صفوفاً لشركة غير الحالية
        with tenant_context(instance):
            ensure_nonprofit_setup(instance)
