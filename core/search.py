"""البحث العام: كلمة واحدة تبحث في كل المستندات والبيانات الأساسية التي يملك المستخدم صلاحية رؤيتها."""
import re

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render

from core.display import display_value, field_label
from core.permissions import path_allowed, scope_queryset
from core.registry import MODULES, REGISTRY

PER_TYPE = 8
SUGGEST_PER_TYPE = 3
SUGGEST_TYPES = 5
SUGGEST_PAGES = 6
MIN_LENGTH = 2
# ترتيب الأنواع في النتائج: الأشخاص والجهات أولاً، ثم المستندات، والبيانات المحاسبية الفنية أخيراً
PEOPLE = {"beneficiary", "donor", "member", "volunteer", "customer", "supplier", "employee", "beneficiary-entity",
          "sponsorship", "waqf", "item", "project", "lead"}
TECHNICAL = {"account", "cost-center", "uom", "item-group", "naming-series"}

# التشكيل والتطويل يُتجاهلان، وصور الحرف الواحد تتطابق (أحمد = احمد، مؤسسة = موسسه، ٥٥ = 55)
MARKS = "ًٌٍَُِّْٰـ"
VARIANTS = ["اأإآٱ", "هة", "يىئ", "وؤ"] + [d + "٠١٢٣٤٥٦٧٨٩"[int(d)] for d in "0123456789"]
CANONICAL = {ch: group for group in VARIANTS for ch in group}
OPTIONAL_MARKS = f"[{MARKS}]*"


PREFIXES = ("وال", "بال", "لل", "ال")
SUFFIXES = ("ات", "ون", "ين", "ية", "يه", "ة", "ه")


def stem(word):
    """جذع تقريبي للكلمة حتى يطابق المفرد الجمع (كفالة ← الكفالات، المستفيدين ← مستفيد)، ولا يقل عن حرفين."""
    for prefix in PREFIXES:
        if word.startswith(prefix) and len(word) - len(prefix) >= 3:
            word = word[len(prefix):]
            break
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


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
        pattern = _word_pattern(stem(word))
        if not pattern:
            continue
        any_field = Q()
        for name in fields:
            any_field |= Q(**{f"{name}__iregex": pattern})
        cond &= any_field
    return cond


def normalize(text):
    """نص موحد للمقارنة: دون تشكيل، والهمزات والتاء المربوطة والألف المقصورة والأرقام الهندية موحدة."""
    text = "".join(ch for ch in str(text or "") if ch not in MARKS).lower()
    return "".join(CANONICAL[ch][0] if ch in CANONICAL else ch for ch in text)


def matches(text, query):
    target = normalize(text)
    return all(stem(normalize(word)) in target for word in query.split())


def is_exact(obj, dt, query):
    """هل يطابق البحث معرّف السجل تماماً (رقم المستند، الكود، الهوية، الجوال، الاسم)؟"""
    wanted = normalize(query.strip())
    for name in ["name", "code", "number"] + list(dt.search_fields):
        if "__" in name:
            continue
        value = getattr(obj, name, None)
        if isinstance(value, str) and value and normalize(value.strip()) == wanted:
            return True
    return False


def _rank(group):
    slug = group["dt"].slug
    return (0 if group["exact"] else 1, 0 if slug in PEOPLE else 2 if slug in TECHNICAL else 1)


def search_doctypes(request, query, per_type=PER_TYPE):
    """[(نوع المستند، النتائج، هل توجد نتائج أكثر)] لكل نوع فيه تطابق ويملك المستخدم الاطلاع عليه، الأقرب أولاً."""
    membership = getattr(request, "membership", None)
    groups = []
    for dt in REGISTRY.values():
        if not dt.search_fields:
            continue
        if membership is not None and not path_allowed(membership, f"/app/{dt.slug}/"):
            continue
        qs = scope_queryset(request, dt.model, dt.model.objects.filter(text_query(dt.search_fields, query))).distinct()
        found = list(qs[:per_type + 1])
        if found:
            exact = [obj for obj in found if is_exact(obj, dt, query)]
            ordered = exact + [obj for obj in found if obj not in exact]
            rows = [{"obj": obj, "cells": [display_value(obj, f) for f in dt.list_fields[:4]]} for obj in ordered[:per_type]]
            groups.append({"dt": dt, "headers": [field_label(dt.model, f) for f in dt.list_fields[:4]],
                           "module": MODULES.get(dt.module, ""), "rows": rows, "exact": exact,
                           "more": len(found) > per_type})
    groups.sort(key=_rank)
    return groups


def search_pages(request, query, limit=None):
    """صفحات القائمة (التقارير والقوائم والإعدادات) التي يطابق اسمها البحث، مع اختصار «إنشاء جديد» للمستندات."""
    from core.navigation import build_menu, menu_pages
    from core.registry import REGISTRY as registry

    membership = getattr(request, "membership", None)
    pages, actions = [], []
    for page in menu_pages(build_menu(request)):
        if not matches(f"{page['label']} {page['section']} {page['module']}", query):
            continue
        page["direct"] = matches(page["label"], query)
        pages.append(page)
        parts = page["url"].split("?")[0].strip("/").split("/")
        dt = registry.get(parts[1]) if len(parts) == 2 and parts[0] == "app" else None
        new_url = f"/app/{parts[1]}/new/" if dt else ""
        if dt and page["direct"] and (membership is None or path_allowed(membership, new_url)):
            actions.append({"label": f"{dt.label} جديد", "url": new_url, "module": page["module"]})
    pages.sort(key=lambda p: not p["direct"])
    return (pages[:limit] if limit else pages), actions


def _exact_hit(groups):
    hits = [obj for group in groups for obj in group["exact"]]
    return hits[0] if len(hits) == 1 else None


@login_required
def search_suggest(request):
    """اقتراحات فورية للمربع أعلى الصفحة: الصفحات، ثم «إنشاء جديد»، ثم أقرب السجلات."""
    query = request.GET.get("q", "").strip()
    if len(query) < MIN_LENGTH:
        return JsonResponse({"groups": []})
    out = []
    groups = search_doctypes(request, query, per_type=SUGGEST_PER_TYPE)
    exact = [{"title": f"{obj}", "url": obj.get_absolute_url(), "meta": g["dt"].label, "kind": "exact"}
             for g in groups for obj in g["exact"][:SUGGEST_PER_TYPE]]
    if exact:
        out.append({"title": "مطابقة تامة", "items": exact[:5]})
    pages, actions = search_pages(request, query, limit=SUGGEST_PAGES)
    if pages:
        out.append({"title": "الصفحات", "items": [
            {"title": p["label"], "url": p["url"], "meta": " › ".join(x for x in (p["module"], p["section"]) if x),
             "kind": "page"} for p in pages]})
    if actions:
        out.append({"title": "إنشاء", "items": [{"title": f"+ {a['label']}", "url": a["url"], "meta": a["module"],
                                                  "kind": "action"} for a in actions[:3]]})
    for group in groups[:SUGGEST_TYPES]:
        items = [{"title": str(row["obj"]), "url": row["obj"].get_absolute_url(),
                  "meta": " · ".join(str(c) for c in row["cells"][1:3] if c not in ("", "–", None))}
                 for row in group["rows"] if row["obj"] not in group["exact"]]
        if items:
            out.append({"title": group["dt"].label_plural, "items": items})
    return JsonResponse({"groups": out})


@login_required
def global_search(request):
    query = request.GET.get("q", "").strip()
    searching = len(query) >= MIN_LENGTH
    groups = search_doctypes(request, query) if searching else []
    pages, actions = search_pages(request, query) if searching else ([], [])
    hit = _exact_hit(groups)
    if hit is not None and not pages and request.GET.get("all") != "1":
        return redirect(hit.get_absolute_url())  # رقم مستند أو هوية مطابقة تماماً: افتحه مباشرة
    return render(request, "core/search.html", {
        "title": "البحث في النظام", "query": query, "groups": groups, "too_short": 0 < len(query) < MIN_LENGTH,
        "pages": pages, "actions": actions, "total": sum(len(g["rows"]) for g in groups) + len(pages),
    })
