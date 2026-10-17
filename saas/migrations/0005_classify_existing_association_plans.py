from django.db import migrations


def classify(apps, schema_editor):
    Plan = apps.get_model('saas', 'Plan')
    Subscription = apps.get_model('saas', 'Subscription')
    db = schema_editor.connection.alias

    def association_plan(original):
        fields = {field.name: getattr(original, field.name) for field in Plan._meta.fields
                  if field.name not in ('id', 'code', 'name', 'organization_type')}
        fields.update(name='الجمعيات — ' + original.name[:85], organization_type='nonprofit', includes_pos=False)
        code = 'nonprofit-' + original.code[:40]
        plan, _ = Plan.objects.using(db).get_or_create(code=code, defaults=fields)
        if plan.organization_type != 'nonprofit':
            raise RuntimeError('تصنيف الباقة ' + code + ' يحتاج مراجعة قبل ترحيل اشتراكات الجمعيات.')
        return plan

    # Existing installations need an association catalog even before the first association signs up.
    for original in Plan.objects.using(db).filter(code__in=['basic', 'pro', 'enterprise'], organization_type='business'):
        association_plan(original)
    for subscription in Subscription.objects.using(db).filter(company__organization_type='nonprofit').select_related('plan'):
        if subscription.plan.organization_type == 'nonprofit':
            continue
        subscription.plan = association_plan(subscription.plan)
        subscription.save(using=db, update_fields=['plan'])


class Migration(migrations.Migration):
    dependencies = [('saas', '0004_plan_is_active_plan_organization_type')]
    operations = [migrations.RunPython(classify, migrations.RunPython.noop)]
