from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import render
from django.utils import timezone

from support.models import MaintenanceContract, MaintenanceVisit, ServiceTicket, TicketStatus


@login_required
def support_dashboard(request):
    now = timezone.now()
    open_tickets = ServiceTicket.objects.exclude(status__in=[TicketStatus.RESOLVED, TicketStatus.CLOSED]).select_related(
        "customer", "assigned_to")
    resolved = ServiceTicket.objects.filter(resolved_at__isnull=False, resolved_at__gte=now - timezone.timedelta(days=30))
    met = sum(1 for t in resolved if t.met_sla)
    today = timezone.localdate()
    return render(request, "support/dashboard.html", {
        "title": "لوحة الصيانة وخدمة العملاء",
        "overdue": [t for t in open_tickets if t.is_overdue], "open_tickets": open_tickets.order_by("due_at"),
        "by_status": open_tickets.values("status").annotate(n=Count("id")), "statuses": dict(TicketStatus.choices),
        "sla_rate": round(met * 100 / len(resolved)) if resolved else None, "resolved_count": len(resolved),
        "visits": MaintenanceVisit.objects.filter(done=False, scheduled_date__lte=today + timezone.timedelta(days=7))
        .select_related("customer", "technician"),
        "expiring": MaintenanceContract.objects.filter(status="active", end_date__gte=today,
                                                       end_date__lte=today + timezone.timedelta(days=30)),
        "today": today,
    })
