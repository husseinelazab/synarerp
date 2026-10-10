from django.db import migrations


def add_defaults(apps, schema_editor):
    """للشركات الموجودة: حساب مصروف مكافأة نهاية الخدمة وربطه مع المخصص، وصرف رصيد الإجازة السنوية."""
    Company = apps.get_model("core", "Company")
    Account = apps.get_model("accounts", "Account")
    LeaveType = apps.get_model("hr", "LeaveType")
    PayrollSettings = apps.get_model("hr", "PayrollSettings")
    for company in Company.objects.all():
        expense = Account.objects.filter(company=company, number="5210", is_group=False).first()
        if expense is None and not Account.objects.filter(company=company, name="مكافأة نهاية الخدمة").exists():
            parent = Account.objects.filter(company=company, number="52", is_group=True).first()
            if parent:
                expense = Account.objects.create(company=company, number="5210", name="مكافأة نهاية الخدمة",
                                                 parent=parent, is_group=False, root_type="expense",
                                                 account_type="expense_account")
        provision = Account.objects.filter(company=company, number="2202", is_group=False).first()
        PayrollSettings.objects.filter(company=company, eos_expense_account__isnull=True).update(
            eos_expense_account=expense)
        PayrollSettings.objects.filter(company=company, eos_provision_account__isnull=True).update(
            eos_provision_account=provision)
        LeaveType.objects.filter(company=company, name="إجازة سنوية").update(encashable=True)


class Migration(migrations.Migration):

    dependencies = [("hr", "0004_eos_attendance")]

    operations = [migrations.RunPython(add_defaults, migrations.RunPython.noop)]
