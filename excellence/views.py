import calendar
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from excellence.models import (KPI, SDG_CHOICES, Initiative, InitiativeStatus, KPIReading, KPIStatus, StrategicPlan,
                               Vision2030Pillar)


def _selected_plan(request):
    pk = request.GET.get("plan", "")
    if pk.isdigit():
        return get_object_or_404(StrategicPlan, pk=pk)
    return StrategicPlan.current()


def _plan_goals(plan):
    """أهداف الخطة مع مبادراتها ومؤشراتها وقراءاتها في استعلامات محدودة."""
    return list(plan.goals.select_related("owner").prefetch_related(
        Prefetch("initiatives", queryset=Initiative.objects.select_related("owner")),
        Prefetch("kpis", queryset=KPI.objects.select_related("owner").prefetch_related("readings"))))


def _kpi_summary(kpis):
    counts = {s: 0 for s in KPIStatus.values}
    for kpi in kpis:
        counts[kpi.status] += 1
    total = len(kpis)
    return {"counts": counts, "total": total,
            "on_target_percent": round(counts[KPIStatus.GREEN] * 100 / total) if total else 0}


@login_required
def excellence_dashboard(request):
    plan = _selected_plan(request)
    context = {"title": "لوحة التميز المؤسسي", "plan": plan,
               "plans": StrategicPlan.objects.all()}
    if plan is not None:
        goals = _plan_goals(plan)
        initiatives = [i for g in goals for i in g.initiatives.all()]
        kpis = [k for g in goals for k in g.kpis.all()]
        context.update({
            "goals": goals, "initiatives_count": len(initiatives), "kpis": kpis,
            "delayed": sorted((i for i in initiatives if i.is_delayed), key=lambda i: i.end_date),
            "plan_progress": plan.progress, "kpi_summary": _kpi_summary(kpis),
        })
    return render(request, "excellence/dashboard.html", context)


def _default_period_end():
    """نهاية الشهر الماضي: الفترة المعتادة لإدخال القراءات."""
    return timezone.localdate().replace(day=1) - datetime.timedelta(days=1)


def _month_end(date):
    return date.replace(day=calendar.monthrange(date.year, date.month)[1])


@login_required
def kpi_entry(request):
    """إدخال قراءات مؤشرات الخطة كلها لفترة واحدة؛ القراءة الموجودة لنفس الفترة تُحدّث."""
    plan = _selected_plan(request)
    period_end = parse_date(request.POST.get("period_end") or request.GET.get("period_end") or "")
    period_end = period_end or _default_period_end()
    kpis = list(KPI.objects.filter(goal__plan=plan).select_related("goal").order_by("goal__code", "id")) if plan else []
    existing = {r.kpi_id: r for r in KPIReading.objects.filter(kpi__in=kpis, period_end=period_end)}
    errors = {}
    if request.method == "POST" and plan is not None:
        saved = 0
        for kpi in kpis:
            raw = request.POST.get(f"value_{kpi.pk}", "").strip().replace(",", "")
            note = request.POST.get(f"note_{kpi.pk}", "").strip()[:255]
            if not raw:
                continue
            try:
                value = Decimal(raw)
                if not value.is_finite() or abs(value) >= Decimal("1e12"):
                    raise InvalidOperation
            except InvalidOperation:
                errors[kpi.pk] = "قيمة غير صحيحة"
                continue
            KPIReading.objects.update_or_create(kpi=kpi, period_end=period_end,
                                                defaults={"value": value, "note": note})
            saved += 1
        if not errors:
            messages.success(request, f"تم حفظ {saved} قراءة لفترة {period_end:%Y-%m-%d}.")
            url = reverse("kpi_entry") + f"?plan={plan.pk}&period_end={period_end:%Y-%m-%d}"
            return redirect(url)
        messages.error(request, "لم تُحفظ بعض القيم؛ راجع الحقول المعلَّمة.")
    rows = []
    for kpi in kpis:
        reading = existing.get(kpi.pk)
        rows.append({"kpi": kpi, "reading": reading, "error": errors.get(kpi.pk),
                     "value": request.POST.get(f"value_{kpi.pk}", "") if errors else (reading.value if reading else ""),
                     "note": request.POST.get(f"note_{kpi.pk}", "") if errors else (reading.note if reading else "")})
    today = timezone.localdate()
    suggestions = [_month_end(datetime.date(today.year - (1 if m > today.month else 0), m, 1))
                   for m in (3, 6, 9, 12)]
    return render(request, "excellence/kpi_entry.html", {
        "title": "إدخال قراءات المؤشرات", "plan": plan, "plans": StrategicPlan.objects.all(),
        "period_end": period_end, "rows": rows, "suggestions": sorted(set(suggestions + [_default_period_end()])),
    })


@login_required
def performance_report(request):
    plan = _selected_plan(request)
    context = {"title": "تقرير الأداء المؤسسي", "plan": plan, "plans": StrategicPlan.objects.all(),
               "as_of": timezone.localdate()}
    if plan is not None:
        goals = _plan_goals(plan)
        kpis = [k for g in goals for k in g.kpis.all()]
        initiatives = [i for g in goals for i in g.initiatives.all()]
        pillars = []
        for value, label in Vision2030Pillar.choices:
            mapped = [g for g in goals if g.vision2030_pillar == value]
            pillars.append({"label": label, "goals": mapped})
        sdg_labels = dict(SDG_CHOICES)
        sdgs = []
        for number in sorted({g.sdg for g in goals if g.sdg}):
            sdgs.append({"label": sdg_labels[number], "goals": [g for g in goals if g.sdg == number]})
        context.update({
            "goals": goals, "plan_progress": plan.progress, "kpi_summary": _kpi_summary(kpis),
            "initiatives_count": len(initiatives),
            "completed_count": sum(1 for i in initiatives if i.status == InitiativeStatus.COMPLETED),
            "delayed_count": sum(1 for i in initiatives if i.is_delayed),
            "budget": sum((i.budget for i in initiatives), Decimal(0)),
            "actual_cost": sum((i.actual_cost for i in initiatives), Decimal(0)),
            "pillars": pillars, "sdgs": sdgs,
            "unmapped": [g for g in goals if not g.vision2030_pillar and not g.sdg],
        })
    return render(request, "excellence/report.html", context)

