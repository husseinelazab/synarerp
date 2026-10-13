"""قيود الصناديق عند الترحيل: لا صرف من صندوق موقوف، ولا من أصل الوقف، ولا بأكثر من رصيد الصندوق المقيد."""
from django.core.exceptions import ValidationError

from core.utils import flt


CLOSING_VOUCHER = "period-closing"

# فروع الدليل المحاسبي الموحد للجهات غير الربحية التي تحدد فئة صافي الأصول عند عدم تحديد صندوق
BRANCH_CLASSES = [("311", "restricted"), ("421", "restricted"), ("23102", "restricted"),
                  ("313", "endowment"), ("43", "endowment"), ("13", "endowment"), ("23103", "endowment")]
# حساب صافي الأصول الذي يُقفل فيه فائض كل فئة
CLASS_NET_ASSETS = {"unrestricted": "23101001", "restricted": "23102001", "endowment": "23103001"}


def branch_class(number):
    return next((cls for prefix, cls in BRANCH_CLASSES if (number or "").startswith(prefix)), "unrestricted")


def row_class(number, fund_type=None):
    """فئة صافي الأصول لسطر قيد: من نوع صندوقه إن وُجد، وإلا من فرع حسابه في الدليل الموحد."""
    return fund_type or branch_class(number)


def check_funds(doc, rows):
    from accounts.models import FundType, RootType

    if getattr(doc, "doctype_slug", "") == CLOSING_VOUCHER:
        return  # قيد الإقفال يعكس أرصدة السنة بصناديقها ولا يُعد صرفاً

    effects = {}
    for row in rows:
        fund = row.get("fund")
        if fund is None:
            continue
        root = row["account"].root_type
        amount = row["credit"] - row["debit"] if root == RootType.INCOME else row["debit"] - row["credit"]
        income, expense = effects.get(fund, (flt(0), flt(0)))
        effects[fund] = (income + amount, expense) if root == RootType.INCOME else (income, expense + amount)

    for fund, (income, expense) in effects.items():
        if not fund.is_active:
            raise ValidationError(f"الصندوق «{fund}» موقوف ولا يقبل قيوداً جديدة.")
        if expense <= 0:
            continue
        if fund.fund_type == FundType.ENDOWMENT:
            raise ValidationError(f"«{fund}» وقف لا يُصرف من أصله. اصرف من صندوق غلة الوقف بدلاً منه.")
        if fund.fund_type != FundType.RESTRICTED:
            continue
        if fund.end_date and doc.posting_date > fund.end_date:
            raise ValidationError(f"انتهت مدة الصرف من الصندوق المقيد «{fund}» في {fund.end_date:%Y-%m-%d}.")
        if fund.allow_overspend:
            continue
        available = fund.totals(exclude_voucher=(doc.doctype_slug, doc.pk))["balance"] + income
        if expense > available:
            raise ValidationError(
                f"رصيد الصندوق المقيد «{fund}» المتاح {flt(available):,.2f} لا يكفي لصرف {flt(expense):,.2f}. "
                "سجّل التبرع للصندوق أولاً أو اصرف من صندوق آخر.")
