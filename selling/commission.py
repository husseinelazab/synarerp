"""تقرير المبيعات والعمولات حسب المندوب والمنطقة، وإضافة العمولات لمسير الرواتب."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import redirect, render

from accounts.reports import _period
from core.utils import flt


def commission_rows(from_date, to_date, territory=None):
    from selling.models import SalesInvoice, SalesPerson

    qs = SalesInvoice.objects.filter(docstatus=1, posting_date__gte=from_date, posting_date__lte=to_date)
    if territory is not None:
        qs = qs.filter(customer__territory_id__in=territory.descendant_ids())
    data = {r["sales_person"]: r for r in qs.values("sales_person").annotate(
        net=Sum("base_net_total"), commission=Sum("commission_amount"), invoices=Count("id"),
        outstanding=Sum("outstanding_amount"), grand=Sum("base_grand_total"))}
    people = SalesPerson.objects.in_bulk([k for k in data if k])
    months = max((to_date.year - from_date.year) * 12 + to_date.month - from_date.month + 1, 1)
    rows = []
    for key, r in data.items():
        person = people.get(key)
        target = flt(person.monthly_target * months) if person else flt(0)
        net = flt(r["net"])
        grand = flt(r["grand"])
        collected = flt(grand - flt(r["outstanding"])) if grand else flt(0)
        rows.append({"person": person, "invoices": r["invoices"], "net": net, "commission": flt(r["commission"]),
                     "collected_percent": flt(collected * 100 / grand) if grand else None, "target": target,
                     "achieved": flt(net * 100 / target) if target else None})
    rows.sort(key=lambda r: -r["net"])
    return rows


def territory_rows(from_date, to_date):
    from selling.models import SalesInvoice

    data = (SalesInvoice.objects.filter(docstatus=1, posting_date__gte=from_date, posting_date__lte=to_date)
            .values("customer__territory__name").annotate(net=Sum("base_net_total"), invoices=Count("id"),
                                                         customers=Count("customer", distinct=True))
            .order_by("-net"))
    return [{"territory": r["customer__territory__name"] or "بدون منطقة", "net": flt(r["net"]),
             "invoices": r["invoices"], "customers": r["customers"]} for r in data]


@login_required
def sales_by_person(request):
    from hr.models import PayrollRun
    from selling.models import Territory

    from_date, to_date = _period(request)
    territory = Territory.objects.filter(pk=request.GET.get("territory") or 0).first()
    rows = commission_rows(from_date, to_date, territory)
    if request.method == "POST" and request.membership.can_manage:
        run = PayrollRun.objects.filter(pk=request.POST.get("payroll_run") or 0, docstatus=0).first()
        if run is None:
            messages.error(request, "اختر مسير رواتب مسودة.")
        else:
            added = add_to_payroll(run, rows, from_date, to_date)
            messages.success(request, f"أُضيفت عمولات {added} مندوب إلى {run}.") if added else \
                messages.warning(request, "لا عمولات جديدة لمندوبين مرتبطين بموظفين في هذا المسير.")
        return redirect(request.get_full_path())
    return render(request, "reports/sales_by_person.html", {
        "title": "المبيعات والعمولات حسب المندوب", "from_date": from_date, "to_date": to_date, "rows": rows,
        "territories": Territory.objects.all(), "territory": territory,
        "by_territory": territory_rows(from_date, to_date),
        "total_net": sum((r["net"] for r in rows), flt(0)),
        "total_commission": sum((r["commission"] for r in rows), flt(0)),
        "payroll_runs": PayrollRun.objects.filter(docstatus=0),
    })


def add_to_payroll(run, rows, from_date, to_date):
    """تُضاف عمولة كل مندوب مرتبط بموظف إلى «الإضافات» في سطره بالمسير (مرة واحدة لكل فترة)."""
    tag = f"عمولة مبيعات {from_date:%Y-%m-%d}/{to_date:%Y-%m-%d}"
    added = 0
    for row in rows:
        person = row["person"]
        if person is None or not person.employee_id or row["commission"] <= 0:
            continue
        line = run.lines.filter(employee_id=person.employee_id).first()
        if line is None or tag in line.notes:
            continue
        line.additions = flt(line.additions + row["commission"])
        line.notes = f"{line.notes} · {tag}: {row['commission']:,.2f}".strip(" ·")[:255]
        line.save(update_fields=["additions", "notes"])
        added += 1
    if added:
        run.calculate_totals()
        run.save()
    return added
