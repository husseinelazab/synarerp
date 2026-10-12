from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone

from core.utils import flt
from crm.models import Activity, Lead, LeadStatus, Opportunity, OpportunityStage


@login_required
def pipeline(request):
    """مسار المبيعات: الفرص المفتوحة في أعمدة حسب المرحلة بقيمها المتوقعة والمرجحة."""
    qs = Opportunity.objects.select_related("customer", "lead", "sales_person")
    person = request.GET.get("sales_person", "")
    if person:
        qs = qs.filter(sales_person_id=person)
    columns = []
    for stage, label in OpportunityStage.choices:
        items = list(qs.filter(stage=stage).order_by("expected_close", "-expected_amount")[:50])
        columns.append({"stage": stage, "label": label, "items": items,
                        "total": sum((o.expected_amount for o in items), flt(0)),
                        "weighted": sum((flt(o.weighted_amount) for o in items), flt(0))})
    open_qs = qs.exclude(stage__in=[OpportunityStage.WON, OpportunityStage.LOST])
    year = timezone.localdate().replace(month=1, day=1)
    closed = qs.filter(closed_on__gte=year)
    won, lost = closed.filter(stage=OpportunityStage.WON).count(), closed.filter(stage=OpportunityStage.LOST).count()
    from selling.models import SalesPerson

    return render(request, "crm/pipeline.html", {
        "title": "مسار المبيعات", "columns": columns, "people": SalesPerson.objects.filter(disabled=False),
        "person": person, "open_total": open_qs.aggregate(t=Sum("expected_amount"))["t"] or 0,
        "forecast": sum((flt(o.weighted_amount) for o in open_qs), flt(0)),
        "win_rate": flt(won * 100 / (won + lost)) if won + lost else None,
        "leads": Lead.objects.values("status").annotate(n=Count("id")),
        "lead_statuses": dict(LeadStatus.choices),
    })


@login_required
def followups(request):
    today = timezone.localdate()
    mine = Activity.objects.filter(done=False)
    if request.GET.get("all") != "1":
        mine = mine.filter(Q(assigned_to=request.user) | Q(assigned_to__isnull=True))
    mine = mine.select_related("lead", "opportunity", "customer", "assigned_to")
    return render(request, "crm/followups.html", {
        "title": "متابعاتي", "overdue": mine.filter(date__lt=today), "today": mine.filter(date=today),
        "upcoming": mine.filter(date__gt=today)[:50], "show_all": request.GET.get("all") == "1",
    })
