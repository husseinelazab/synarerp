from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from awqaf.models import InstallmentStatus, LeaseStatus, RentInstallment, RentReceipt, Waqf, WaqfLease
from core.utils import flt


@login_required
def awqaf_dashboard(request):
    today = timezone.localdate()
    waqfs = list(Waqf.objects.filter(is_active=True).select_related("principal_fund", "revenue_fund"))
    overdue = [i for i in RentInstallment.objects.filter(
        status__in=[InstallmentStatus.PENDING, InstallmentStatus.PARTIAL], due_date__lt=today).select_related(
        "lease__waqf")]
    expiring = WaqfLease.objects.filter(status=LeaseStatus.ACTIVE, end_date__gte=today,
                                        end_date__lte=today + timezone.timedelta(days=60)).select_related("waqf")
    return render(request, "awqaf/dashboard.html", {
        "title": "لوحة إدارة الأوقاف", "today": today, "waqfs": waqfs,
        "asset_total": flt(sum((w.asset_value for w in waqfs), flt(0))),
        "contracted": flt(WaqfLease.objects.filter(status=LeaseStatus.ACTIVE).aggregate(t=Sum("annual_rent"))["t"]),
        "collected_year": flt(RentReceipt.objects.filter(docstatus=1, posting_date__year=today.year)
                              .aggregate(t=Sum("amount"))["t"]),
        "overdue": overdue, "overdue_total": flt(sum((i.outstanding for i in overdue), flt(0))),
        "expiring": expiring,
    })
