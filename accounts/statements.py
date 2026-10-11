"""قائمة التدفقات النقدية والقوائم المالية المقارنة (فترات سابقة، أشهر، فروع)."""
import calendar
import datetime
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render

from accounts.models import Account, AccountType, CashFlowCategory, CostCenter, GLEntry, RootType
from accounts.reports import CLOSING_VOUCHER, _active_gl, _cost_center_filter, _level, _period, _sums
from core.utils import flt

CASH_TYPES = (AccountType.CASH, AccountType.BANK)

# حسابات تُجمع في بند واحد حتى لا تظهر الحركات غير النقدية المتقابلة داخل نفس المستند (تكلفة/مخزون)
BUCKETS = {
    AccountType.RECEIVABLE: "المتحصل من العملاء (صافي)",
    AccountType.PAYABLE: "المدفوع للموردين (صافي)",
    AccountType.STOCK: "المخزون وتكلفة البضاعة",
    AccountType.COGS: "المخزون وتكلفة البضاعة",
    AccountType.STOCK_ADJUSTMENT: "المخزون وتكلفة البضاعة",
    AccountType.TAX: "ضريبة القيمة المضافة",
}


def cash_flow_data(from_date, to_date):
    """التدفقات بالطريقة المباشرة: كل مستند حرّك النقدية يُنسب صافي حركته للحسابات المقابلة فيه."""
    cash_ids = set(Account.objects.filter(account_type__in=CASH_TYPES).values_list("id", flat=True))
    period = _active_gl().filter(posting_date__gte=from_date, posting_date__lte=to_date)
    vouchers = set(period.filter(account_id__in=cash_ids).values_list("voucher_type", "voucher_id"))
    lines = OrderedDict((c, OrderedDict()) for c in CashFlowCategory.values)
    accounts = {a.pk: a for a in Account.objects.all()}
    by_voucher = {}
    for row in (period.exclude(account_id__in=cash_ids).values("voucher_type", "voucher_id", "account_id")
                .annotate(d=Sum("debit"), c=Sum("credit"))):
        if (row["voucher_type"], row["voucher_id"]) in vouchers:
            by_voucher.setdefault((row["voucher_type"], row["voucher_id"]), []).append(row)
    for rows in by_voucher.values():
        for row in rows:
            account = accounts[row["account_id"]]
            amount = flt(row["c"]) - flt(row["d"])  # الدائن المقابل مصدر نقدية، والمدين استخدام لها
            if not amount:
                continue
            category = account.effective_cash_flow_category
            label = BUCKETS.get(account.account_type) if category == CashFlowCategory.OPERATING else None
            key = label or str(account)
            entry = lines[category].setdefault(key, {"label": key, "amount": flt(0),
                                                     "account": None if label else account})
            entry["amount"] += amount
    sections = []
    for category, items in lines.items():
        rows = [r for r in items.values() if r["amount"]]
        rows.sort(key=lambda r: -abs(r["amount"]))
        sections.append({"key": category, "label": CashFlowCategory(category).label, "rows": rows,
                         "total": sum((r["amount"] for r in rows), flt(0))})

    def cash_balance(before):
        agg = _active_gl().filter(account_id__in=cash_ids, posting_date__lt=before).aggregate(
            d=Sum("debit"), c=Sum("credit"))
        return flt(agg["d"]) - flt(agg["c"])

    opening = cash_balance(from_date)
    closing = cash_balance(to_date + datetime.timedelta(days=1))
    net = sum((s["total"] for s in sections), flt(0))
    return {"sections": sections, "opening": opening, "closing": closing, "net": net,
            "reconciles": opening + net == closing}


@login_required
def cash_flow(request):
    from_date, to_date = _period(request)
    data = cash_flow_data(from_date, to_date)
    return render(request, "reports/cash_flow.html", {"title": "قائمة التدفقات النقدية", "from_date": from_date,
                                                      "to_date": to_date, **data})


# ---------- القوائم المقارنة ----------

def _shift_years(date, years):
    try:
        return date.replace(year=date.year + years)
    except ValueError:  # 29 فبراير
        return date.replace(year=date.year + years, day=28)


def _month_end(date):
    return date.replace(day=calendar.monthrange(date.year, date.month)[1])


def comparison_periods(mode, from_date, to_date, as_of=False):
    """أعمدة المقارنة: [(العنوان، من، إلى، مراكز التكلفة)]."""
    fmt = "%Y-%m-%d"
    if mode == "monthly":
        periods, start = [], from_date.replace(day=1)
        while start <= to_date and len(periods) < 24:
            end = min(_month_end(start), to_date)
            periods.append((start.strftime("%Y-%m"), max(start, from_date), end, None))
            start = end + datetime.timedelta(days=1)
        return periods
    current = ("الفترة الحالية" if not as_of else f"في {to_date:{fmt}}", from_date, to_date, None)
    if mode == "previous_year":
        prev = (f"العام السابق" if not as_of else f"في {_shift_years(to_date, -1):{fmt}}",
                _shift_years(from_date, -1), _shift_years(to_date, -1), None)
    else:  # الفترة السابقة بنفس الطول
        length = (to_date - from_date).days + 1
        prev_to = from_date - datetime.timedelta(days=1)
        if as_of:
            prev_to = _month_end(to_date.replace(day=1) - datetime.timedelta(days=1))
        prev = ("الفترة السابقة" if not as_of else f"في {prev_to:{fmt}}",
                prev_to - datetime.timedelta(days=length - 1), prev_to, None)
    return [current, prev]


def center_periods(from_date, to_date):
    centers = CostCenter.objects.filter(is_group=False, disabled=False).order_by("name")
    return [(str(c), from_date, to_date, [c.pk]) for c in centers] + [("الإجمالي", from_date, to_date, None)]


def multi_column(root_types, periods, as_of=False, level=None):
    """أقسام القائمة بعدة أعمدة: لكل قسم صفوف الشجرة بقيمة لكل عمود، والإجمالي لكل عمود."""
    from accounts.tree import tree_rows

    width = len(periods)
    values = {}
    roots = dict(Account.objects.values_list("id", "root_type"))
    for index, (_label, start, end, centers) in enumerate(periods):
        qs = _active_gl().filter(posting_date__lte=end, account__root_type__in=root_types)
        if not as_of:
            qs = qs.filter(posting_date__gte=start).exclude(voucher_type=CLOSING_VOUCHER)
        if centers is not None:
            qs = qs.filter(cost_center_id__in=centers)
        for account_id, (d, c) in _sums(qs).items():
            values.setdefault(account_id, [flt(0)] * width)
            credit_nature = roots[account_id] in (RootType.INCOME, RootType.LIABILITY, RootType.EQUITY)
            values[account_id][index] = (c - d) if credit_nature else (d - c)
    sections = []
    for root in root_types:
        rows = tree_rows(values, width, root_types=[root], max_depth=level)
        totals = [flt(0)] * width
        for row in rows:
            if row["depth"] == 0:
                totals = [t + v for t, v in zip(totals, row["values"])]
        sections.append({"root": root, "label": RootType(root).label, "rows": [r for r in rows if r["depth"] > 0],
                         "totals": totals})
    return sections


def _variance(columns):
    """الفرق ونسبة التغير بين العمودين الأول والثاني (للمقارنة بفترة سابقة)."""
    if len(columns) != 2:
        return None
    current, previous = columns
    diff = current - previous
    return {"diff": diff, "percent": flt(diff * 100 / abs(previous)) if previous else None}


@login_required
def comparative_pnl(request):
    from accounts.tree import max_tree_depth

    from_date, to_date = _period(request)
    mode = request.GET.get("mode", "previous_period")
    level = _level(request)
    if mode == "by_center":
        periods = center_periods(from_date, to_date)
    else:
        periods = comparison_periods(mode, from_date, to_date)
    income, expense = multi_column([RootType.INCOME, RootType.EXPENSE], periods, level=level)
    result = [i - e for i, e in zip(income["totals"], expense["totals"])]
    for section in (income, expense):
        for row in section["rows"]:
            row["variance"] = _variance(row["values"]) if mode in ("previous_period", "previous_year") else None
    return render(request, "reports/statement_compare.html", {
        "title": "قائمة الدخل المقارنة", "from_date": from_date, "to_date": to_date, "mode": mode,
        "modes": [("previous_period", "مقابل الفترة السابقة"), ("previous_year", "مقابل نفس الفترة من العام السابق"),
                  ("monthly", "شهرياً"), ("by_center", "حسب الفروع / مراكز التكلفة")],
        "headers": [p[0] for p in periods], "sections": [income, expense], "result_label": "صافي الربح / (الخسارة)",
        "result": result, "show_variance": mode in ("previous_period", "previous_year"),
        "result_variance": _variance(result), "level": level or 0, "levels": range(2, max_tree_depth() + 1),
    })


@login_required
def comparative_balance_sheet(request):
    from accounts.tree import max_tree_depth

    from_date, to_date = _period(request)
    mode = request.GET.get("mode", "previous_year")
    if mode not in ("previous_year", "previous_period", "monthly"):
        mode = "previous_year"
    level = _level(request)
    if mode == "monthly":
        periods = [(label, start, end, None) for label, start, end, _c in comparison_periods("monthly", from_date, to_date)]
    else:
        periods = comparison_periods(mode, from_date, to_date, as_of=True)
    assets, liabilities, equity = multi_column([RootType.ASSET, RootType.LIABILITY, RootType.EQUITY], periods,
                                               as_of=True, level=level)
    income, expense = multi_column([RootType.INCOME, RootType.EXPENSE], periods, as_of=True)
    profit = [i - e for i, e in zip(income["totals"], expense["totals"])]
    if any(profit):
        equity["rows"].append({"account": "أرباح (خسائر) غير مرحلة", "values": profit, "depth": 1, "is_group": False})
        equity["totals"] = [t + p for t, p in zip(equity["totals"], profit)]
    for section in (assets, liabilities, equity):
        for row in section["rows"]:
            row["variance"] = _variance(row["values"]) if mode != "monthly" else None
    right = [l + e for l, e in zip(liabilities["totals"], equity["totals"])]
    return render(request, "reports/statement_compare.html", {
        "title": "الميزانية العمومية المقارنة", "from_date": from_date, "to_date": to_date, "mode": mode, "as_of": True,
        "modes": [("previous_year", "مقابل نهاية العام السابق"), ("previous_period", "مقابل نهاية الشهر السابق"),
                  ("monthly", "في نهاية كل شهر")],
        "headers": [p[0] for p in periods], "sections": [assets, liabilities, equity],
        "result_label": "الخصوم + حقوق الملكية", "result": right, "show_variance": mode != "monthly",
        "result_variance": _variance(right), "balanced": all(a == r for a, r in zip(assets["totals"], right)),
        "level": level or 0, "levels": range(2, max_tree_depth() + 1),
    })
