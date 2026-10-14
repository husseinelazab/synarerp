from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def excellence_dashboard(request):
    return render(request, "excellence/dashboard.html", {"title": "لوحة التميز المؤسسي"})
