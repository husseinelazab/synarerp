"""تقارير الأموال: أرصدة الصناديق، وقائمة الأنشطة حسب القيود على صافي الأصول."""
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import render

from accounts.funds import CLOSING_VOUCHER, row_class
from accounts.models import Account, Fund, FundType, GLEntry, RootType
from accounts.reports import _period
from core.utils import flt

# أعمدة قائمة الأنشطة: غير المقيد يشمل ما لا يحمل صندوقاً
CLASSES = [(FundType.UNRESTRICTED, "غير مقيدة"), (FundType.RESTRICTED, "مقيدة"), (FundType.ENDOWMENT, "أوقاف")]


def _income_expense():
    """إيرادات ومصروفات السنوات دون قيود الإقفال (الإقفال ينقل الصافي لصافي الأصول ولا يُعد نشاطاً)."""
    return GLEntry.objects.filter(is_cancelled=False, account__root_type__in=[RootType.INCOME, RootType.EXPENSE]
                                  ).exclude(voucher_type=CLOSING_VOUCHER)


def _movement(rows):
    """(الوارد، المصروف) لمجموعة قيود: الإيراد دائن − مدين، والمصروف مدين − دائن."""
    sums = {r["account__root_type"]: (flt(r["d"]), flt(r["c"])) for r in
            rows.values("account__root_type").annotate(d=Sum("debit"), c=Sum("credit"))}
    income = sums.get(RootType.INCOME, (0, 0))
    expense = sums.get(RootType.EXPENSE, (0, 0))
    return flt(income[1] - income[0]), flt(expense[0] - expense[1])


def fund_balances_data(from_date, to_date):
    entries = _income_expense()
    rows = []
    general = [("الأموال العامة (بدون صندوق)", FundType.UNRESTRICTED, None, Q(fund__isnull=True))]
    funds = [(f.name, f.fund_type, f, Q(fund=f)) for f in Fund.objects.all()]
    for label, fund_type, fund, condition in general + funds:
        scoped = entries.filter(condition)
        received_before, spent_before = _movement(scoped.filter(posting_date__lt=from_date))
        received, spent = _movement(scoped.filter(posting_date__gte=from_date, posting_date__lte=to_date))
        opening = received_before - spent_before
        row = {"label": label, "fund": fund, "fund_type": FundType(fund_type).label, "opening": opening,
               "received": received, "spent": spent, "closing": opening + received - spent}
        if fund is None or fund.is_active or any(row[k] for k in ("opening", "received", "spent")):
            rows.append(row)
    totals = {k: sum((r[k] for r in rows), flt(0)) for k in ("opening", "received", "spent", "closing")}
    by_type = OrderedDict((label, flt(0)) for _, label in CLASSES)
    for row in rows:
        key = next(label for value, label in CLASSES if FundType(value).label == row["fund_type"])
        by_type[key] += row["closing"]
    return {"rows": rows, "totals": totals, "by_type": by_type}


def activities_data(from_date, to_date):
    period = _income_expense().filter(posting_date__gte=from_date, posting_date__lte=to_date)
    sums = {}
    for r in period.values("account_id", "account__number", "account__root_type", "fund__fund_type").annotate(
            d=Sum("debit"), c=Sum("credit")):
        cls = row_class(r["account__number"], r["fund__fund_type"])
        sign = 1 if r["account__root_type"] == RootType.INCOME else -1
        amount = flt((flt(r["c"]) - flt(r["d"])) * sign)
        sums.setdefault(r["account_id"], {}).setdefault(cls, flt(0))
        sums[r["account_id"]][cls] += amount
    accounts = Account.objects.filter(pk__in=sums).order_by("number", "name")
    sections = []
    for root, title in ((RootType.INCOME, "الإيرادات والتبرعات"), (RootType.EXPENSE, "المصروفات")):
        lines = []
        for account in accounts:
            if account.root_type != root:
                continue
            values = [sums[account.pk].get(cls, flt(0)) for cls, _ in CLASSES]
            lines.append({"account": account, "values": values, "total": sum(values, flt(0))})
        totals = [sum((line["values"][i] for line in lines), flt(0)) for i in range(len(CLASSES))]
        sections.append({"title": title, "lines": lines, "totals": totals, "total": sum(totals, flt(0))})
    income, expense = sections
    net = [income["totals"][i] - expense["totals"][i] for i in range(len(CLASSES))]
    return {"columns": [label for _, label in CLASSES], "sections": sections, "net": net,
            "net_total": sum(net, flt(0))}


@login_required
def fund_balances(request):
    from_date, to_date = _period(request)
    return render(request, "funds/fund_balances.html", {
        "title": "أرصدة الصناديق والأموال المقيدة", "from_date": from_date, "to_date": to_date,
        **fund_balances_data(from_date, to_date)})


@login_required
def statement_of_activities(request):
    from_date, to_date = _period(request)
    return render(request, "funds/activities.html", {
        "title": "قائمة الأنشطة", "from_date": from_date, "to_date": to_date,
        **activities_data(from_date, to_date)})
