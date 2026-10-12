"""البحث العام: كلمة واحدة تبحث في كل المستندات والبيانات الأساسية التي يملك المستخدم صلاحية رؤيتها."""
import re

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render

from core.display import display_value, field_label
from core.permissions import path_allowed, scope_queryset
from core.registry import MODULES, REGISTRY

PER_TYPE = 8
MIN_LENGTH = 2

# التشكيل والتطويل يُتجاهلان، وصور الحرف الواحد تتطابق (أحمد = احمد، مؤسسة = موسسه، ٥٥ = 55)
MARKS = "ًٌٍَُِّْٰـ"
VARIANTS = ["اأإآٱ", "هة", "يىئ", "وؤ"] + [d + "٠١٢٣٤٥٦٧٨٩"[int(d)] for d in "0123456789"]
CANONICAL = {ch: group for group in VARIANTS for ch in group}
OPTIONAL_MARKS = f"[{MARKS}]*"


def _word_pattern(word):
    """تعبير نمطي يطابق الكلمة مهما كان تشكيلها أو صورة همزتها في النص المخزن."""
    parts = []
    for ch in word:
        if ch in MARKS:
            continue
        group = CANONICAL.get(ch)
        parts.append(f"[{group}]" if group else re.escape(ch))
    return OPTIONAL_MARKS.join(parts)


def text_query(fields, query):
    """كل كلمة من البحث يجب أن توجد في أحد الحقول (بغض النظر عن التشكيل والهمزات وحالة الأحرف)."""
    cond = Q()
    for word in query.split():
        pattern = _word_pattern(word)
        if not pattern:
            continue
        any_field = Q()
        for name in fields:
            any_field |= Q(**{f"{name}__iregex": pattern})
        cond &= any_field
    return cond


def search_doctypes(request, query):
    """[(نوع المستند، النتائج، هل توجد نتائج أكثر)] لكل نوع فيه تطابق ويملك المستخدم الاطلاع عليه."""
    membership = getattr(request, "membership", None)
    groups = []
    for dt in REGISTRY.values():
        if not dt.search_fields:
            continue
        if membership is not None and not path_allowed(membership, f"/app/{dt.slug}/"):
            continue
        qs = scope_queryset(request, dt.model, dt.model.objects.filter(text_query(dt.search_fields, query))).distinct()
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
