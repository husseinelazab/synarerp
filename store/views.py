from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def store_dashboard(request):
    return render(request, "store/dashboard.html", {"title": "لوحة المتجر الإلكتروني"})
