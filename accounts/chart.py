"""تطبيق دليل الحسابات السعودي الموحد: يُنشئ ما ينقص من حسابات الدليل دون تعديل أو حذف أي حساب قائم."""
from django.db import transaction

from core.setup_data import ACCOUNT_ATTRIBUTES, CHART_OF_ACCOUNTS, NONPROFIT_CHART


@transaction.atomic
def apply_unified_chart(company, chart=None):
    """يعيد (عدد الحسابات المضافة، [حسابات تعذرت إضافتها مع السبب])."""
    from accounts.models import Account

    existing = {a.number: a for a in Account.all_objects.filter(company=company) if a.number}
    names = set(Account.all_objects.filter(company=company).values_list("name", flat=True))
    created, skipped = 0, []

    def walk(nodes, parent, root_type):
        nonlocal created
        for number, name, kind, children in nodes:
            is_root = parent is None
            node_root = kind if is_root else root_type
            account = existing.get(number)
            if account is None:
                if name in names:
                    skipped.append(f"{number} - {name}: يوجد حساب آخر بنفس الاسم")
                    continue
                if parent is not None and not parent.is_group:
                    skipped.append(f"{number} - {name}: الحساب الأب {parent} ليس مجموعة")
                    continue
                flow, zakat = ACCOUNT_ATTRIBUTES.get(number, ("", ""))
                account = Account.all_objects.create(
                    company=company, number=number, name=name, parent=parent, is_group=children is not None,
                    root_type=node_root,
                    account_type="" if (is_root or children is not None) else (kind or ""),
                    cash_flow_category=flow, zakat_treatment=zakat)
                existing[number], created = account, created + 1
                names.add(name)
            if children:
                if not account.is_group:
                    skipped.append(f"{number} - {account.name}: حساب فرعي عليه قيود ولا يمكن أن يصبح مجموعة")
                    continue
                walk(children, account, node_root)

    walk(chart or CHART_OF_ACCOUNTS, None, None)
    return created, skipped


def apply_nonprofit_chart(company):
    """حسابات الجمعيات: صافي الأصول، إيرادات التبرعات والزكاة والمنح، ومصروفات البرامج."""
    return apply_unified_chart(company, chart=NONPROFIT_CHART)
