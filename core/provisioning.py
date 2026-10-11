"""تأسيس شركة جديدة على المنصة بكل بياناتها الأساسية."""
import datetime

from django.db import transaction
from django.utils import timezone

from core.models import Company, Currency, FiscalYear, Membership, Role
from core.setup_data import CHART_OF_ACCOUNTS, DEFAULT_ACCOUNTS, DEPARTMENTS, DESIGNATIONS, ITEM_GROUPS, UOMS
from core.tenancy import tenant_context


CURRENCIES = [
    ("SAR", "ريال سعودي", "ر.س"), ("AED", "درهم إماراتي", "د.إ"), ("KWD", "دينار كويتي", "د.ك"),
    ("QAR", "ريال قطري", "ر.ق"), ("BHD", "دينار بحريني", "د.ب"), ("OMR", "ريال عماني", "ر.ع"),
    ("EGP", "جنيه مصري", "ج.م"), ("JOD", "دينار أردني", "د.أ"), ("USD", "دولار أمريكي", "$"), ("EUR", "يورو", "€"),
]


def ensure_currencies():
    for code, name, symbol in CURRENCIES:
        Currency.objects.get_or_create(code=code, defaults={"name": name, "symbol": symbol})


def default_currency():
    ensure_currencies()
    return Currency.objects.get(code="SAR")


def _create_accounts(nodes, parent, root_type):
    from accounts.models import Account

    for number, name, kind, children in nodes:
        is_root = parent is None
        node_root = kind if is_root else root_type
        account = Account.objects.create(
            number=number, name=name, parent=parent, is_group=children is not None, root_type=node_root,
            account_type="" if (is_root or children is not None) else (kind or ""),
        )
        if children:
            _create_accounts(children, account, node_root)


@transaction.atomic
def provision_company(name, owner=None, abbr="", tax_id="", phone="", plan=None, demo=False):
    """ينشئ الشركة ودليل حساباتها ومستودعاتها وإعدادات نقطة البيع، ويربط المالك بها ويبدأ اشتراكها."""
    from accounts.models import Account
    from hr.models import Department, Designation
    from pos.models import POSProfile
    from selling.models import Customer
    from stock.models import UOM, ItemGroup, Warehouse

    company = Company.objects.create(name=name, abbr=abbr or name[:10], tax_id=tax_id, phone=phone,
                                     currency=default_currency())
    with tenant_context(company):
        _create_accounts(CHART_OF_ACCOUNTS, None, None)
        accounts = {a.number: a for a in Account.objects.all()}
        Account.objects.filter(number="2201").update(cash_flow_category="financing")
        for field, number in DEFAULT_ACCOUNTS.items():
            setattr(company, field, accounts[number])
        company.save()

        from accounts.models import CostCenter

        root = CostCenter.objects.create(name="جميع المراكز", is_group=True)
        company.default_cost_center = CostCenter.objects.create(name="الفرع الرئيسي", parent=root)
        company.save(update_fields=["default_cost_center"])

        year = timezone.localdate().year
        FiscalYear.objects.create(name=str(year), start_date=datetime.date(year, 1, 1), end_date=datetime.date(year, 12, 31))
        for uom in UOMS:
            UOM.objects.create(name=uom)
        for group in ITEM_GROUPS:
            ItemGroup.objects.create(name=group)
        main = Warehouse.objects.create(name="المستودع الرئيسي")
        Warehouse.objects.create(name="مستودع الفرع")
        for department in DEPARTMENTS:
            Department.objects.create(name=department)
        for designation in DESIGNATIONS:
            Designation.objects.create(name=designation)
        from hr.models import DEFAULT_LEAVE_TYPES, LeaveType, PayrollSettings, add_official_holidays

        add_official_holidays(year)
        from assets.models import DEFAULT_CATEGORIES, AssetCategory

        for category, number, months in DEFAULT_CATEGORIES:
            AssetCategory.objects.create(name=category, asset_account=accounts[number], useful_life_months=months,
                                         accumulated_depreciation_account=accounts["1204"],
                                         depreciation_expense_account=accounts["5208"])
        from hr.models import DEFAULT_APPRAISAL_CRITERIA, AppraisalTemplate

        template = AppraisalTemplate.objects.create(name="تقييم الأداء السنوي")
        for criterion, weight in DEFAULT_APPRAISAL_CRITERIA:
            template.criteria.create(name=criterion, weight=weight)

        for leave_name, days, paid in DEFAULT_LEAVE_TYPES:
            LeaveType.objects.create(name=leave_name, days_per_year=days, is_paid=paid,
                                     encashable=leave_name == "إجازة سنوية")
        PayrollSettings.for_company(company)
        walk_in = Customer.objects.create(name="عميل نقدي", customer_type="individual")
        POSProfile.objects.create(
            name="الكاشير الرئيسي", warehouse=main, default_customer=walk_in,
            cash_account=company.default_cash_account, card_account=accounts["1102"],
            tax_rate=company.default_tax_rate,
        )
        if demo:
            from core.demo import create_demo_data

            create_demo_data(company)

    if owner is not None:
        Membership.objects.create(user=owner, company=company, role=Role.OWNER)

    from saas.services import start_subscription

    start_subscription(company, plan)
    return company
