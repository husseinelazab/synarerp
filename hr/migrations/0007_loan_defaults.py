from django.db import migrations


def add_defaults(apps, schema_editor):
    """للشركات الموجودة: حساب سلف الموظفين وربطه بإعدادات الرواتب."""
    Company = apps.get_model("core", "Company")
    Account = apps.get_model("accounts", "Account")
    PayrollSettings = apps.get_model("hr", "PayrollSettings")
    for company in Company.objects.all():
        account = Account.objects.filter(company=company, name="سلف الموظفين").first()
        if account is None:
            parent = Account.objects.filter(company=company, number="11", is_group=True).first()
            if parent:
                # الرقم 1107 قد يكون مستخدماً لحساب آخر أضافته الشركة؛ نأخذ أول رقم متاح
                used = set(Account.objects.filter(company=company).values_list("number", flat=True))
                number = next((str(n) for n in range(1107, 1200) if str(n) not in used), "")
                account = Account.objects.create(company=company, number=number, name="سلف الموظفين", parent=parent,
                                                 is_group=False, root_type="asset", account_type="")
        if account is not None and account.is_group:
            account = None
        PayrollSettings.objects.filter(company=company, loan_account__isnull=True).update(loan_account=account)


class Migration(migrations.Migration):

    dependencies = [("hr", "0006_loans_holidays_shifts")]

    operations = [migrations.RunPython(add_defaults, migrations.RunPython.noop)]
