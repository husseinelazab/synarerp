from django.db import migrations

CATEGORIES = [("الأثاث والمفروشات", "1201", 120), ("الأجهزة والمعدات", "1202", 60), ("السيارات", "1203", 60)]


def _account(Account, company, name, parent_number, root_type, account_type, preferred):
    account = Account.objects.filter(company=company, name=name).first()
    if account is None:
        parent = Account.objects.filter(company=company, number=parent_number, is_group=True).first()
        if parent is None:
            return None
        used = set(Account.objects.filter(company=company).values_list("number", flat=True))
        number = next((str(n) for n in range(preferred, preferred + 90) if str(n) not in used), "")
        account = Account.objects.create(company=company, number=number, name=name, parent=parent, is_group=False,
                                         root_type=root_type, account_type=account_type)
    return account


def add_defaults(apps, schema_editor):
    """للشركات الموجودة: مجمع الإهلاك وحساب أرباح وخسائر الاستبعاد وفئات الأصول الافتراضية."""
    Company = apps.get_model("core", "Company")
    Account = apps.get_model("accounts", "Account")
    AssetCategory = apps.get_model("assets", "AssetCategory")
    for company in Company.objects.all():
        accumulated = _account(Account, company, "مجمع إهلاك الأصول الثابتة", "12", "asset",
                               "accumulated_depreciation", 1204)
        _account(Account, company, "أرباح وخسائر استبعاد الأصول", "4", "income", "income_account", 4105)
        expense = Account.objects.filter(company=company, name="الإهلاك", is_group=False).first()
        if accumulated is None or expense is None or AssetCategory.objects.filter(company=company).exists():
            continue
        for name, number, months in CATEGORIES:
            asset_account = Account.objects.filter(company=company, number=number, is_group=False).first()
            if asset_account:
                AssetCategory.objects.create(company=company, name=name, asset_account=asset_account,
                                             accumulated_depreciation_account=accumulated,
                                             depreciation_expense_account=expense, useful_life_months=months)


class Migration(migrations.Migration):

    dependencies = [("assets", "0001_initial"), ("accounts", "0009_alter_account_account_type")]

    operations = [migrations.RunPython(add_defaults, migrations.RunPython.noop)]
