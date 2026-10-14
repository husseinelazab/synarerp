from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def relations_dashboard(request):
    return render(request, "relations/dashboard.html", {"title": "لوحة العلاقات العامة والإعلام"})
