from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from core.utils import flt
from grants.models import Grant, GrantInstallment, GrantReport, GrantStatus, ReportStatus


def dashboard_data(today=None):
    """أرقام لوحة المنح للشركة الحالية."""
    today = today or timezone.localdate()
    soon = today + timezone.timedelta(days=30)
    grants = list(Grant.objects.filter(status=GrantStatus.ACTIVE).select_related("grantor", "fund")
                  .prefetch_related("installments__receipt"))
    rows = [{"grant": g, "approved": flt(g.approved_amount), "received": g.received_amount, "spent": g.spent_amount,
             "balance": g.fund_balance, "burn": g.burn_rate, "elapsed": g.time_elapsed} for g in grants]
    pending = GrantInstallment.objects.filter(receipt__isnull=True, grant__status=GrantStatus.ACTIVE)
    reports = GrantReport.objects.filter(status=ReportStatus.PENDING).select_related("grant")
    return {
        "today": today, "rows": rows, "active_count": len(grants),
        "approved_total": flt(sum((r["approved"] for r in rows), flt(0))),
        "received_total": flt(sum((r["received"] for r in rows), flt(0))),
        "spent_total": flt(sum((r["spent"] for r in rows), flt(0))),
        "upcoming_installments": list(pending.filter(due_date__gte=today, due_date__lte=soon).select_related("grant")),
        "overdue_installments": list(pending.filter(due_date__lt=today).select_related("grant")),
        "reports_due": list(reports.filter(due_date__gte=today, due_date__lte=soon)),
        "reports_overdue": list(reports.filter(due_date__lt=today)),
    }


@login_required
def grants_dashboard(request):
    data = dashboard_data()
    data["upcoming_total"] = flt(sum((i.amount for i in data["upcoming_installments"]), flt(0)))
    return render(request, "grants/dashboard.html", {"title": "لوحة إدارة المنح", **data})


@login_required
def grant_financial_report(request, pk):
    grant = get_object_or_404(Grant.objects.select_related("grantor", "fund", "project"), pk=pk)
    today = timezone.localdate()
    return render(request, "grants/financial_report.html", {
        "title": f"التقرير المالي للمانح — {grant.title}", "grant": grant,
        "from_date": grant.start_date, "to_date": min(grant.end_date, today) if grant.start_date <= today
        else grant.end_date,
        "bva": grant.budget_vs_actual(), "summary": grant.financial_summary(),
        "installments": grant.installments.select_related("receipt"),
    })
