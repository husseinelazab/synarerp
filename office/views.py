from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from core.permissions import scope_queryset
from office.models import (AdminRequest, ArchiveDocument, Circular, CircularAck, Correspondence,
                           CorrespondenceStatus, Direction)


def _correspondence(request, pk):
    return get_object_or_404(scope_queryset(request, Correspondence, Correspondence.objects.all()), pk=pk)


@login_required
@require_POST
def route_correspondence(request, pk):
    item = _correspondence(request, pk)
    to_user = get_user_model().objects.filter(
        pk=request.POST.get("to_user") or 0, memberships__company=request.company,
        memberships__is_active=True).first()
    try:
        item.route(request.user, to_user, request.POST.get("note", "").strip(),
                   parse_date(request.POST.get("due_date") or ""))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, f"أُحيلت المعاملة إلى {to_user.get_full_name() or to_user}.")
    return redirect(item.get_absolute_url())


@login_required
def letter(request, pk):
    from core.models import PrintSettings

    item = _correspondence(request, pk)
    return render(request, "office/letter.html", {"item": item, "company": request.company,
                                                  "ps": PrintSettings.for_company(request.company)})


@login_required
def documents_dashboard(request):
    today = timezone.localdate()
    open_items = scope_queryset(request, Correspondence, Correspondence.objects.exclude(
        status=CorrespondenceStatus.CLOSED)).select_related("assigned_to")
    return render(request, "office/documents_dashboard.html", {
        "title": "لوحة إدارة التوثيق والمستندات", "today": today,
        "incoming_open": open_items.filter(direction=Direction.INCOMING).count(),
        "outgoing_year": Correspondence.objects.filter(direction=Direction.OUTGOING, date__year=today.year).count(),
        "overdue": [c for c in open_items if c.is_overdue],
        "open_items": open_items.order_by("due_date", "-date")[:50],
        "expiring": ArchiveDocument.objects.filter(expiry_date__isnull=False,
                                                   expiry_date__lte=today + timezone.timedelta(days=30))
        .order_by("expiry_date")[:20],
    })


@login_required
def my_desk(request):
    """المكتب الإلكتروني: كل ما يخص المستخدم في صفحة واحدة."""
    from beneficiaries.models import AidDisbursement
    from core.models import Notification
    from governance.models import DecisionStatus, MeetingDecision
    from projects.models import ProjectTask, TaskStatus

    user, today = request.user, timezone.localdate()
    circulars = Circular.objects.filter(date__lte=today).exclude(expires_on__lt=today)
    acked = set(CircularAck.objects.filter(user=user, circular__in=circulars).values_list("circular_id", flat=True))
    from hr.models import Employee

    employee = Employee.objects.filter(user=user).first()
    return render(request, "office/desk.html", {
        "title": "مكتبي", "today": today,
        "correspondence": Correspondence.objects.filter(assigned_to=user).exclude(
            status=CorrespondenceStatus.CLOSED).order_by("due_date", "-date"),
        "tasks": ProjectTask.objects.filter(assigned_to=user, status__in=[TaskStatus.OPEN, TaskStatus.WORKING])
        .select_related("project").order_by("due_date"),
        "decisions": MeetingDecision.objects.filter(responsible=user, status=DecisionStatus.OPEN)
        .select_related("meeting").order_by("due_date"),
        "requests": AdminRequest.objects.filter(created_by=user).select_related("request_type")[:10],
        "notifications": Notification.objects.filter(user=user, is_read=False).order_by("-id")[:10],
        "circulars": [(c, c.pk in acked) for c in circulars[:10]],
        "pending_circulars": sum(1 for c in circulars if c.requires_ack and c.pk not in acked),
        "aid_waiting": AidDisbursement.objects.filter(docstatus=0, created_by=user).count(),
        "employee": employee,
    })


@login_required
@require_POST
def acknowledge_circular(request, pk):
    circular = get_object_or_404(Circular, pk=pk)
    circular.acknowledge(request.user)
    messages.success(request, "تم تأكيد اطلاعك على التعميم.")
    return redirect(request.POST.get("next") or "my_desk")
