from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def programs_dashboard(request):
    return render(request, "programs/dashboard.html", {"title": "لوحة البرامج والتطوير"})
