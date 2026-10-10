from django.db import migrations

CRITERIA = [
    ("جودة العمل ودقته", 25),
    ("الإنتاجية وإنجاز المهام في وقتها", 25),
    ("الالتزام بالدوام والأنظمة", 15),
    ("العمل الجماعي والتعاون", 15),
    ("المبادرة والتطوير الذاتي", 10),
    ("التعامل مع العملاء والزملاء", 10),
]


def add_defaults(apps, schema_editor):
    """للشركات الموجودة: نموذج تقييم الأداء السنوي الافتراضي."""
    Company = apps.get_model("core", "Company")
    AppraisalTemplate = apps.get_model("hr", "AppraisalTemplate")
    AppraisalCriterion = apps.get_model("hr", "AppraisalCriterion")
    for company in Company.objects.all():
        if AppraisalTemplate.objects.filter(company=company).exists():
            continue
        template = AppraisalTemplate.objects.create(company=company, name="تقييم الأداء السنوي")
        for name, weight in CRITERIA:
            AppraisalCriterion.objects.create(parent=template, name=name, weight=weight)


class Migration(migrations.Migration):

    dependencies = [("hr", "0010_notifications_appraisals")]

    operations = [migrations.RunPython(add_defaults, migrations.RunPython.noop)]
