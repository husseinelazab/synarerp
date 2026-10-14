from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def fleet_dashboard(request):
    return render(request, "fleet/dashboard.html", {"title": "لوحة الحركة والصيانة"})
