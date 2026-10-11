"""الموازنات التقديرية: الرقابة عند ترحيل المصروفات، وتقرير الفعلي مقابل الموازنة."""
import datetime

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render

from core.utils import flt, money

CLOSING_VOUCHER = "period-closing"


def check_budgets(doc, rows):
    """يُستدعى قبل حفظ قيود مستند: يمنع أو ينبّه إن تجاوز مصروف حساباً في موازنة معتمدة."""
    from accounts.models import Budget, BudgetAction, BudgetLine, RootType

    if getattr(doc, "doctype_slug", "") == CLOSING_VOUCHER:
        return
    spending = {}
    for row in rows:
        if row["account"].root_type == RootType.EXPENSE and row["debit"] > 0:
            key = (row["account"].pk, getattr(row["cost_center"], "pk", None))
            spending[key] = spending.get(key, flt(0)) + row["debit"]
    if not spending:
        return
    lines = BudgetLine.objects.filter(
        parent__company=doc.company, parent__docstatus=1, parent__fiscal_year__start_date__lte=doc.posting_date,
        parent__fiscal_year__end_date__gte=doc.posting_date, account_id__in={key[0] for key in spending},
    ).exclude(parent__action=BudgetAction.NONE).select_related("parent__fiscal_year", "parent__cost_center", "account")
    warnings = []
    for line in lines:
        budget = line.parent
        centers = budget.center_ids()
        amount = sum((value for (account_id, center_id), value in spending.items()
                      if account_id == line.account_id and (centers is None or center_id in centers)), flt(0))
        if not amount:
            continue
        actual = budget.actuals().get(line.account_id, flt(0))
        if actual + amount <= line.amount:
            continue
        scope = f" ({budget.cost_center})" if budget.cost_center_id else ""
        message = (f"تجاوز موازنة {line.account}{scope} لسنة {budget.fiscal_year}: الموازنة {money(line.amount)}، "
                   f"المصروف حتى الآن {money(actual)}، وهذا المستند يضيف {money(amount)}.")
        if budget.action == BudgetAction.STOP:
            raise ValidationError(message)
        warnings.append((message, budget))
    if warnings:
        from core.notify import notify_managers

        company, url = doc.company, doc.get_absolute_url()
        for message, budget in warnings:
            transaction.on_commit(lambda m=message: notify_managers(company, f"{doc.name}: {m}", url))


def budget_report_rows(budget, to_date):
    """لكل حساب: الموازنة السنوية، المستهدف حتى التاريخ (توزيع شهري متساوٍ)، الفعلي، الفرق ونسبة الاستهلاك."""
    year = budget.fiscal_year
    end = min(max(to_date, year.start_date), year.end_date)
    total_months = (year.end_date.year - year.start_date.year) * 12 + year.end_date.month - year.start_date.month + 1
    elapsed = (end.year - year.start_date.year) * 12 + end.month - year.start_date.month + 1
    share = flt(elapsed) / total_months
    actuals = budget.actuals(end, exclude_voucher=CLOSING_VOUCHER)
    rows = []
    for line in budget.lines.select_related("account"):
        actual = actuals.get(line.account_id, flt(0))
        to_date_budget = flt(line.amount * share)
        rows.append({
            "account": line.account, "amount": line.amount, "to_date": to_date_budget, "actual": actual,
            "variance": flt(to_date_budget - actual), "remaining": flt(line.amount - actual),
            "used": (actual / line.amount * 100).quantize(flt("0.1")) if line.amount else None,
        })
    return rows, end, elapsed, total_months


@login_required
def budget_variance(request):
    from accounts.models import Budget

    budgets = Budget.objects.filter(docstatus=1).select_related("fiscal_year", "cost_center").order_by(
        "-fiscal_year__start_date", "cost_center__name")
    budget = budgets.filter(pk=request.GET.get("budget") or 0).first() or budgets.first()
    to_date = datetime.date.today()
    try:
        to_date = datetime.date.fromisoformat(request.GET.get("to_date") or "") or to_date
    except ValueError:
        pass
    context = {"title": "الموازنة مقابل الفعلي", "budgets": budgets, "budget": budget, "to_date": to_date}
    if budget:
        rows, end, elapsed, months = budget_report_rows(budget, to_date)
        totals = {key: sum((r[key] for r in rows), flt(0)) for key in ("amount", "to_date", "actual", "variance",
                                                                         "remaining")}
        context.update(rows=rows, totals=totals, end=end, elapsed=elapsed, months=months,
                       used=(totals["actual"] / totals["amount"] * 100).quantize(flt("0.1")) if totals["amount"] else None)
    return render(request, "reports/budget_variance.html", context)
