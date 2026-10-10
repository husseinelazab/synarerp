from django.db import migrations

NAME = "بضائع مستلمة لم تصل فواتيرها"


def add_account(apps, schema_editor):
    """للشركات الموجودة: الحساب الوسيط لأذون الاستلام تحت الخصوم المتداولة."""
    Company = apps.get_model("core", "Company")
    Account = apps.get_model("accounts", "Account")
    for company in Company.objects.all():
        if Account.objects.filter(company=company, name=NAME).exists():
            continue
        parent = Account.objects.filter(company=company, number="21", is_group=True).first()
        if parent is None:
            continue
        used = set(Account.objects.filter(company=company).values_list("number", flat=True))
        number = next((str(n) for n in range(2106, 2190) if str(n) not in used), "")
        Account.objects.create(company=company, number=number, name=NAME, parent=parent, is_group=False,
                               root_type="liability", account_type="")


class Migration(migrations.Migration):

    dependencies = [("buying", "0006_delivery_receipts"), ("accounts", "0009_alter_account_account_type")]

    operations = [migrations.RunPython(add_account, migrations.RunPython.noop)]
