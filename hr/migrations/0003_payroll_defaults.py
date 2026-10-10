from django.db import migrations

LEAVE_TYPES = [
    ("إجازة سنوية", 21, True),
    ("إجازة مرضية", 30, True),
    ("إجازة بدون راتب", 0, False),
    ("إجازة زواج", 5, True),
    ("إجازة وفاة", 5, True),
    ("إجازة مولود", 3, True),
    ("إجازة امتحانات", 0, True),
]
NEW_ACCOUNTS = [
    # (الرقم، الاسم، رقم الأب، النوع الجذري، نوع الحساب)
    ("2105", "التأمينات الاجتماعية المستحقة", "21", "liability", ""),
    ("5209", "التأمينات الاجتماعية - حصة المنشأة", "52", "expense", "expense_account"),
]


def add_defaults(apps, schema_editor):
    """للشركات الموجودة: حسابات التأمينات وأنواع الإجازات وتحديد الموظفين السعوديين."""
    Company = apps.get_model("core", "Company")
    Account = apps.get_model("accounts", "Account")
    LeaveType = apps.get_model("hr", "LeaveType")
    Employee = apps.get_model("hr", "Employee")
    for company in Company.objects.all():
        for number, name, parent_number, root_type, account_type in NEW_ACCOUNTS:
            if Account.objects.filter(company=company, number=number).exists() or \
                    Account.objects.filter(company=company, name=name).exists():
                continue
            parent = Account.objects.filter(company=company, number=parent_number, is_group=True).first()
            if parent:
                Account.objects.create(company=company, number=number, name=name, parent=parent, is_group=False,
                                       root_type=root_type, account_type=account_type)
        for name, days, paid in LEAVE_TYPES:
            if not LeaveType.objects.filter(company=company, name=name).exists():
                LeaveType.objects.create(company=company, name=name, days_per_year=days, is_paid=paid)
    for employee in Employee.objects.all():
        if employee.nationality.strip() in ("سعودي", "سعودية", "السعودية", "Saudi", "saudi", "SA"):
            employee.is_saudi = True
            employee.save(update_fields=["is_saudi"])


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0002_employee_bank_name_employee_gosi_subscribed_and_more"),
        ("accounts", "0008_paymententry_base_amount_paymententry_currency_and_more"),
    ]

    operations = [migrations.RunPython(add_defaults, migrations.RunPython.noop)]
