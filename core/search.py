"""البحث العام: كلمة واحدة تبحث في كل المستندات والبيانات الأساسية التي يملك المستخدم صلاحية رؤيتها."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render

from core.display import display_value, field_label
from core.permissions import path_allowed, scope_queryset
from core.registry import MODULES, REGISTRY

PER_TYPE = 8
MIN_LENGTH = 2


def search_doctypes(request, query):
    """[(نوع المستند، النتائج، هل توجد نتائج أكثر)] لكل نوع فيه تطابق ويملك المستخدم الاطلاع عليه."""
    membership = getattr(request, "membership", None)
    groups = []
    for dt in REGISTRY.values():
        if not dt.search_fields:
            continue
        if membership is not None and not path_allowed(membership, f"/app/{dt.slug}/"):
            continue
        cond = Q()
        for name in dt.search_fields:
            cond |= Q(**{f"{name}__icontains": query})
        qs = scope_queryset(request, dt.model, dt.model.objects.filter(cond)).distinct()
        found = list(qs[:PER_TYPE + 1])
        if found:
            rows = [{"obj": obj, "cells": [display_value(obj, f) for f in dt.list_fields[:4]]} for obj in found[:PER_TYPE]]
            groups.append({"dt": dt, "headers": [field_label(dt.model, f) for f in dt.list_fields[:4]], "module": MODULES.get(dt.module, ""), "rows": rows,
                           "more": len(found) > PER_TYPE})
    return groups


@login_required
def global_search(request):
    query = request.GET.get("q", "").strip()
    groups = search_doctypes(request, query) if len(query) >= MIN_LENGTH else []
    return render(request, "core/search.html", {
        "title": "البحث في النظام", "query": query, "groups": groups, "too_short": 0 < len(query) < MIN_LENGTH,
        "total": sum(len(g["rows"]) for g in groups),
    })
