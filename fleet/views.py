import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from core.models import DocStatus
from core.permissions import scope_queryset
from core.utils import flt
from fleet.models import (EXPIRY_WARNING_DAYS, OPEN_MAINTENANCE, FuelLog, MaintenanceRequest, MaintenancePriority,
                          MaintenanceStatus, MaintenanceTarget, TripStatus, Vehicle, VehicleRequest, VehicleStatus)
PRIORITY_ORDER = {MaintenancePriority.EMERGENCY: 0, MaintenancePriority.URGENT: 1, MaintenancePriority.NORMAL: 2}


@login_required
def fleet_dashboard(request):
    today = timezone.localdate()
    now = timezone.now()
    vehicles = list(Vehicle.objects.select_related("driver"))
    approved = VehicleRequest.objects.filter(docstatus=DocStatus.SUBMITTED, status=TripStatus.APPROVED) \
        .select_related("vehicle", "created_by")
    open_maintenance = sorted(MaintenanceRequest.objects.filter(status__in=OPEN_MAINTENANCE)
                              .select_related("vehicle", "assigned_to"),
                              key=lambda m: (PRIORITY_ORDER.get(m.priority, 9), m.date))
    fuel_month = FuelLog.objects.filter(date__year=today.year, date__month=today.month)
    return render(request, "fleet/dashboard.html", {
        "title": "لوحة الحركة والصيانة", "today": today,
        "in_service": sum(1 for v in vehicles if v.status == VehicleStatus.ACTIVE),
        "vehicles_count": len(vehicles),
        "pending": VehicleRequest.objects.filter(docstatus=DocStatus.DRAFT).select_related("vehicle", "created_by")
        .order_by("posting_date", "id")[:50],
        "today_trips": approved.filter(start_at__date__lte=today, end_at__date__gte=today).count(),
        "upcoming": approved.filter(end_at__gte=now - datetime.timedelta(hours=12),
                                    start_at__date__lte=today + datetime.timedelta(days=7)).order_by("start_at")[:30],
        "open_maintenance": open_maintenance,
        "fuel_month": flt(fuel_month.aggregate(t=Sum("amount"))["t"]),
        "attention": [v for v in vehicles if v.expiring_documents or v.service_due],
        "warning_days": EXPIRY_WARNING_DAYS,
    })


@login_required
@require_POST
def complete_trip(request, pk):
    trip = get_object_or_404(scope_queryset(request, VehicleRequest, VehicleRequest.objects.all()), pk=pk)
    try:
        reading = int(request.POST.get("end_odometer") or "")
    except ValueError:
        messages.error(request, "أدخل قراءة العداد عند العودة رقماً صحيحاً.")
        return redirect(trip.get_absolute_url())
    try:
        with transaction.atomic():
            trip.complete_trip(request.user, reading)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, f"تم إنهاء المشوار: {trip.distance} كم، وحُدِّث عداد السيارة.")
    return redirect(trip.get_absolute_url())


def _period(request):
    today = timezone.localdate()
    from_date = parse_date(request.GET.get("from_date") or "") or today.replace(day=1)
    to_date = parse_date(request.GET.get("to_date") or "") or today
    return from_date, to_date


@login_required
def fleet_report(request):
    """تقرير الحركة والصيانة لفترة: لكل سيارة المشاوير والمسافة والوقود والصيانة وتكلفة الكيلومتر، وصيانة المرافق."""
    from_date, to_date = _period(request)
    trips = VehicleRequest.objects.filter(docstatus=DocStatus.SUBMITTED, status=TripStatus.DONE,
                                          start_at__date__gte=from_date, start_at__date__lte=to_date)
    fuel = FuelLog.objects.filter(date__gte=from_date, date__lte=to_date)
    done = MaintenanceRequest.objects.filter(status=MaintenanceStatus.DONE, completed_on__gte=from_date,
                                             completed_on__lte=to_date)
    trip_rows = {}
    for t in trips.values("vehicle_id", "start_odometer", "end_odometer"):
        row = trip_rows.setdefault(t["vehicle_id"], [0, 0])
        row[0] += 1
        if t["end_odometer"] is not None and t["start_odometer"] is not None:
            row[1] += t["end_odometer"] - t["start_odometer"]
    fuel_rows = {r["vehicle_id"]: r for r in fuel.values("vehicle_id").annotate(l=Sum("liters"), a=Sum("amount"))}
    mnt_rows = {r["vehicle_id"]: r for r in done.filter(target=MaintenanceTarget.VEHICLE, vehicle__isnull=False)
                .values("vehicle_id").annotate(n=Count("id"), c=Sum("cost"))}
    rows, totals = [], {"trips": 0, "km": 0, "liters": 0, "fuel": 0, "maint_count": 0, "maint": 0, "cost": 0}
    for vehicle in Vehicle.objects.all():
        trips_n, km = trip_rows.get(vehicle.pk, (0, 0))
        f = fuel_rows.get(vehicle.pk, {})
        m = mnt_rows.get(vehicle.pk, {})
        row = {"vehicle": vehicle, "trips": trips_n, "km": km, "liters": flt(f.get("l")), "fuel": flt(f.get("a")),
               "maint_count": m.get("n", 0), "maint": flt(m.get("c"))}
        row["cost"] = row["fuel"] + row["maint"]
        row["per_km"] = flt(row["cost"] / km) if km else None
        if any(row[k] for k in ("trips", "liters", "fuel", "maint_count")):
            rows.append(row)
            for key in totals:
                totals[key] += row[key]
    totals["per_km"] = flt(totals["cost"] / totals["km"]) if totals["km"] else None
    facility = done.filter(target=MaintenanceTarget.FACILITY).values("category").annotate(n=Count("id"), c=Sum("cost")) \
        .order_by("category")
    labels = dict(MaintenanceRequest._meta.get_field("category").choices)
    facility_rows = [{"category": labels.get(r["category"], r["category"]), "count": r["n"], "cost": flt(r["c"])}
                     for r in facility]
    return render(request, "fleet/report.html", {
        "title": "تقرير الحركة والصيانة", "from_date": from_date, "to_date": to_date, "company": request.company,
        "rows": rows, "totals": totals, "facility_rows": facility_rows,
        "facility_total": {"count": sum(r["count"] for r in facility_rows),
                           "cost": flt(sum(r["cost"] for r in facility_rows))},
    })
