"""رقابة الموازنات عند الترحيل وتقرير الموازنة مقابل الفعلي."""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.utils import timezone

from core.utils import flt


def _ancestor_ids(account):
    ids, node = [], account
    while node is not None and node.pk not in ids:
        ids.append(node.pk)
        node = node.parent
    return ids


def check_budgets(doc, rows):
    """بعد تجهيز قيود المستند وقبل حفظها: يُمنع أو يُنبَّه إن تجاوز مصروف الموازنة المعتمدة."""
    from accounts.models import BudgetAction, BudgetLine, RootType

    expense_rows = [r for r in rows if r["account"].root_type == RootType.EXPENSE and r["debit"] > 0]
    if not expense_rows:
        return
    lines = (BudgetLine.objects.filter(parent__company=doc.company, parent__disabled=False,
                                       parent__fiscal_year__start_date__lte=doc.posting_date,
                                       parent__fiscal_year__end_date__gte=doc.posting_date)
             .select_related("parent", "parent__fiscal_year", "parent__cost_center", "account"))
    lines = list(lines)
    if not lines:
        return
    from core.notify import notify_managers

    for row in expense_rows:
        ancestors = set(_ancestor_ids(row["account"]))
        center = row.get("cost_center")
        for line in lines:
            if line.account_id not in ancestors:
                continue
            budget = line.parent
            centers = budget.center_ids()
            if centers is not None and (center is None or center.pk not in centers):
                continue
            allowed = budget.allowed(line.amount, doc.posting_date)
            spent = budget.actual(line.account, doc.posting_date if budget.control_period == "monthly" else None)
            after = spent + flt(row["debit"])
            if after <= allowed:
                continue
            message = (f"تجاوز الموازنة «{budget}» على {line.account}: المسموح {allowed:,.2f}، المصروف {spent:,.2f}، "
                       f"وهذا المستند ({doc}) يضيف {row['debit']:,.2f}.")
            if budget.action == BudgetAction.STOP:
                raise ValidationError(message + " اطلب رفع الموازنة أو غيّر مركز التكلفة.")
            notify_managers(doc.company, message, doc.get_absolute_url() if hasattr(doc, "get_absolute_url") else "")


@login_required
def budget_report(request):
    from accounts.models import Budget

    budgets = Budget.objects.filter(disabled=False).select_related("fiscal_year", "cost_center")
    budget = budgets.filter(pk=request.GET.get("budget") or 0).first() or budgets.first()
    today = timezone.localdate()
    rows, totals = [], {"budget": flt(0), "allowed": flt(0), "actual": flt(0)}
    if budget is not None:
        until = min(today, budget.fiscal_year.end_date)
        for line in budget.lines.select_related("account").order_by("account__number"):
            actual = budget.actual(line.account)
            allowed = budget.allowed(line.amount, until)
            used = flt(actual * 100 / line.amount) if line.amount else None
            rows.append({"account": line.account, "budget": line.amount, "allowed": allowed, "actual": actual,
                         "remaining": flt(line.amount - actual), "used": used, "over": actual > allowed})
            totals["budget"] += line.amount
            totals["allowed"] += allowed
            totals["actual"] += actual
    totals["remaining"] = totals["budget"] - totals["actual"]
    return render(request, "reports/budget.html", {"title": "الموازنة مقابل الفعلي", "budgets": budgets,
                                                   "budget": budget, "rows": rows, "totals": totals})
