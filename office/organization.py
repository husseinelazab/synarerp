from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from hr.models import OrganizationUnit


@login_required
def chart(request):
    if not request.company.is_nonprofit:
        raise Http404
    rows = list(OrganizationUnit.objects.filter(company=request.company).select_related("role_policy"))
    nodes = {row.pk:{"row":row,"children":[]} for row in rows}
    roots = []
    for row in rows:
        if row.parent_id in nodes:
            nodes[row.parent_id]["children"].append(nodes[row.pk])
        else:
            roots.append(nodes[row.pk])
    return render(request,"office/organization.html",{"roots":roots,"count":len(rows)})
