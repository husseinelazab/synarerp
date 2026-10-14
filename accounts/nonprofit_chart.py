"""الدليل المحاسبي الموحد للجهات غير الربحية (2026) — يُؤسس به الجمعيات بدلاً من دليل المنشآت التجارية.

البيانات الأصلية في data/nonprofit_chart.csv (الرمز، الاسم، الأب) كما وردت في الدليل.
فروقات التطبيق عن الملف الأصلي:
- «23 صافي الأصول» جذر مستقل (نوعه صافي أصول) بدلاً من فرع تحت «2 الالتزامات وصافي الأصول»، لأن النظام يفصل
  الالتزامات عن صافي الأصول في القوائم ويشترط أن يتبع الحساب الفرعي نوع أبيه.
- الأسماء المكررة في الدليل (مثل «الرواتب والأجور النقدية - مستحقة» تحت أكثر من أب) يُضاف إليها اسم الأب بين قوسين
  لأن اسم الحساب لا يتكرر في الشركة.
- حسابات تكميلية يحتاجها النظام وليست في الدليل (SUPPLEMENTARY): الأرصدة الافتتاحية، فروق الصرف، تكلفة المبيعات
  وتسويات المخزون والخصم المسموح به.
"""
import csv
from collections import Counter
from pathlib import Path

from django.db import transaction

DATA_FILE = Path(__file__).parent / "data" / "nonprofit_chart.csv"

SUPPLEMENTARY = [
    ("23104", "الأرصدة الافتتاحية", "231"),
    ("23104001", "الأرصدة الافتتاحية - حساب وسيط", "23104"),
    ("31205009", "فروق أسعار الصرف", "31205"),
    ("41406", "تكلفة المبيعات وتسويات المخزون", "414"),
    ("41406001", "تكلفة البضاعة المباعة والموزعة", "41406"),
    ("41406002", "تسويات وفروقات المخزون", "41406"),
    ("41406003", "الخصم المسموح به", "41406"),
]

# الحسابات الافتراضية للشركة
DEFAULT_ACCOUNTS = {
    "default_cash_account": "11103001",          # الصندوق العام
    "default_receivable_account": "11402001",    # عملاء - مدينون تجاريون
    "default_stock_account": "11702001",         # بضائع ومنتجات للتوزيع الخيري
    "default_input_tax_account": "11405001",     # ضريبة القيمة المضافة مشتريات
    "default_payable_account": "21302001",       # موردون - تجاريون
    "default_output_tax_account": "21305001",    # ضريبة القيمة المضافة - مبيعات
    "default_income_account": "31205002",        # مبيعات السلع والخدمات
    "default_cogs_account": "41406001",
    "default_stock_adjustment_account": "41406002",
    "default_expense_account": "41201009",       # مستهلكات أخرى
    "default_discount_account": "41406003",
    "default_opening_account": "23104001",
    "default_retained_earnings_account": "23101001",  # صافي الأصول غير المقيدة
    "default_exchange_account": "31205009",
}

BANK_ACCOUNT = "11101001"

# إعدادات الرواتب: نفس مفاتيح دليل المنشآت التجارية في hr.models.default_accounts
PAYROLL_ACCOUNTS = {
    "5201": "41101001",  # الرواتب والأجور الأساسية
    "5209": "41101008",  # تأمينات اجتماعية (مصروف)
    "5210": "41102020",  # تعويضات نهاية الخدمة
    "2202": "22401001",  # مخصص مكافآت نهاية الخدمة
    "2103": "21401001",  # الرواتب والأجور النقدية - مستحقة
    "2105": "21304004",  # تأمينات إجتماعية (التزام)
    "1102": BANK_ACCOUNT,
    "1107": "11401004",  # ذمم العاملين - السلف والقروض
}

# فئات الأصول: (الاسم، الأصل، مجمع الإهلاك، مصروف الإهلاك، العمر بالأشهر)
ASSET_CATEGORIES = [
    ("الأثاث المكتبي", "12105001", "22501005", "41305001", 120),
    ("الآلات والمعدات المكتبية", "12106001", "22501006", "41306001", 60),
    ("أجهزة الحاسب الآلي وملحقاتها", "12110001", "22501010", "41310001", 48),
    ("السيارات", "12104002", "2250100401", "41304002", 60),
    ("المباني", "12102002", "2250100201", "41302002", 360),
    ("المباني الوقفية", "13402001", "2270100201", "43103001", 360),
]

ACCOUNT_TYPES = [  # (بادئة الرمز، النوع) — الأطول تطابقاً يُطبق أولاً، على الحسابات الفرعية فقط
    ("11101", "bank"), ("11102", "bank"), ("13101", "bank"),
    ("11103", "cash"), ("11104", "cheques_receivable"), ("11402002", "cheques_receivable"),
    ("11402", "receivable"),
    ("117", "stock"),
    ("11405", "tax"), ("21305", "tax"), ("21601", "tax"),
    ("121", "fixed_asset"), ("134", "fixed_asset"),
    ("225", "accumulated_depreciation"), ("227", "accumulated_depreciation"),
    ("21302", "payable"), ("212", "cheques_payable"),
    ("23", "equity"), ("3", "income_account"), ("4", "expense_account"),
]
CASH_FLOW = [("211", "financing"), ("221", "financing"), ("222", "financing"),
             ("113", "investing"), ("123", "investing"), ("133", "investing"), ("132", "investing")]


def root_type(code):
    if code.startswith("23"):
        return "equity"
    return {"1": "asset", "2": "liability", "3": "income", "4": "expense"}[code[0]]


def _by_prefix(code, rules):
    matches = [(prefix, value) for prefix, value in rules if code.startswith(prefix)]
    return max(matches, key=lambda m: len(m[0]))[1] if matches else ""


def chart_rows():
    """صفوف الدليل بعد التطبيق: (الرمز، الاسم الفريد، الرمز الأب أو None)."""
    with DATA_FILE.open(encoding="utf-8") as f:
        rows = [(r["code"], r["name"], r["parent"] or None) for r in csv.DictReader(f)]
    rows += SUPPLEMENTARY
    rows = [(code, name, None if code == "23" else parent) for code, name, parent in rows]
    names = {code: name for code, name, _ in rows}
    counts = Counter(name for _, name, _ in rows)
    unique, seen = [], set()
    for code, name, parent in rows:
        if counts[name] > 1:
            name = f"{name} ({names[parent]})" if parent else name
        if name in seen:
            name = f"{name} - {code}"
        seen.add(name)
        unique.append((code, name[:150], parent))
    return unique


@transaction.atomic
def apply_nonprofit_chart(company):
    """يُنشئ ما ينقص من حسابات الدليل بأرقامها دون تعديل أو حذف أي حساب قائم. يعيد (عدد المضاف، [ما تعذر])."""
    from accounts.models import Account

    rows = chart_rows()
    parents = {parent for _, _, parent in rows if parent}
    existing = {a.number: a for a in Account.all_objects.filter(company=company) if a.number}
    names = set(Account.all_objects.filter(company=company).values_list("name", flat=True))
    created, skipped = 0, []
    for code, name, parent_code in rows:  # الآباء تسبق الأبناء في الملف
        if code in existing:
            continue
        if name in names:
            skipped.append(f"{code} - {name}: يوجد حساب آخر بنفس الاسم")
            continue
        parent = existing.get(parent_code) if parent_code else None
        if parent_code and (parent is None or not parent.is_group):
            skipped.append(f"{code} - {name}: الحساب الأب {parent_code} غير موجود أو ليس مجموعة")
            continue
        is_group = code in parents
        existing[code] = Account.all_objects.create(
            company=company, number=code, name=name, parent=parent, is_group=is_group, root_type=root_type(code),
            account_type="" if is_group else _by_prefix(code, ACCOUNT_TYPES),
            cash_flow_category=_by_prefix(code, CASH_FLOW))
        names.add(name)
        created += 1
    return created, skipped


def setup_company_accounts(company):
    """تأسيس جمعية جديدة: الدليل، والحسابات الافتراضية، وفئات الأصول. يعيد قاموس الحسابات بأرقامها."""
    from accounts.models import Account
    from assets.models import AssetCategory

    apply_nonprofit_chart(company)
    accounts = {a.number: a for a in Account.all_objects.filter(company=company)}
    for field, number in DEFAULT_ACCOUNTS.items():
        setattr(company, field, accounts[number])
    company.save()
    for name, asset, accumulated, expense, months in ASSET_CATEGORIES:
        AssetCategory.all_objects.create(company=company, name=name, asset_account=accounts[asset],
                                         accumulated_depreciation_account=accounts[accumulated],
                                         depreciation_expense_account=accounts[expense], useful_life_months=months)
    return accounts
