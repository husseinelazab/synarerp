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


from django.db.models.signals import m2m_changed
from django.core.exceptions import ValidationError
from beneficiaries.models import DependentCategory, FamilyMember


@receiver(m2m_changed, sender=FamilyMember.categories.through)
def validate_dependent_categories(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    if reverse:
        allowed = set(FamilyMember.objects.filter(pk__in=pk_set, parent__company_id=instance.company_id).values_list("pk", flat=True))
        invalid = allowed != pk_set
    else:
        allowed = set(DependentCategory.all_objects.filter(pk__in=pk_set, company_id=instance._company_id()).values_list("pk", flat=True))
        invalid = allowed != pk_set
    if invalid:
        raise ValidationError("تصنيف التابع لا يخص هذه الجمعية.")
