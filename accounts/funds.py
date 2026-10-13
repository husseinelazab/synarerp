"""قيود الصناديق عند الترحيل: لا صرف من صندوق موقوف، ولا من أصل الوقف، ولا بأكثر من رصيد الصندوق المقيد."""
from django.core.exceptions import ValidationError

from core.utils import flt


def check_funds(doc, rows):
    from accounts.models import FundType, RootType

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
