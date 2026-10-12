from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.utils import flt
from projects.models import Project, ProjectStatus


@login_required
def project_report(request):
    """ربحية المشاريع: الإيراد والتكلفة من القيود، ونسبة الإنجاز، والمفوتر، والمتبقي للفوترة، ومقارنة بالموازنة."""
    status = request.GET.get("status", "active")
    projects = Project.objects.select_related("customer")
    if status in ProjectStatus.values:
        projects = projects.filter(status=status)
    rows = []
    for p in projects:
        totals = p.gl_totals()
        progress = p.progress
        billed = p.billed_amount()
        earned = flt(flt(p.contract_value) * progress / 100)
        rows.append({"project": p, "progress": progress, **totals, "billed": billed,
                     "to_bill": max(earned - billed, flt(0)) if p.contract_value else flt(0),
                     "budget_used": flt(totals["cost"] * 100 / p.cost_budget) if p.cost_budget else None})
    totals = {k: sum((r[k] for r in rows), flt(0)) for k in ("revenue", "cost", "profit", "billed", "to_bill")}
    return render(request, "projects/report.html", {"title": "ربحية المشاريع", "rows": rows, "totals": totals,
                                                    "status": status, "statuses": ProjectStatus.choices})
