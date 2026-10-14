"""القوائم المنسدلة الكبيرة (العملاء، الأصناف، المستفيدون...) تُبحث على الخادم بدل تحميل كل خياراتها في الصفحة.

عند تجاوز عدد الخيارات الحد يُعرض في الصفحة الخيار المختار فقط، ويحمل العنصر رمزاً يجلب به المتصفح النتائج أثناء
الكتابة. يُحفظ استعلام الحقل نفسه (بفلاتر الشركة والنموذج والصلاحيات) في الذاكرة المؤقتة مربوطاً بالمستخدم والشركة،
فلا يرى المتصفح إلا ما كان سيظهر له في القائمة الكاملة.
"""
import hashlib
import os
import pickle
import time

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.urls import reverse

from core.tenancy import get_current_company, get_current_user

THRESHOLD = int(os.environ.get("SYNAR_REMOTE_SELECT_THRESHOLD", "300"))
RESULTS = 30
FALLBACK_SEARCH_FIELDS = ("name", "code", "full_name", "title", "account_name", "national_id", "phone", "mobile")

# عدد خيارات كل استعلام لمدة قصيرة: أسطر الجدول في النموذج الواحد تشترك في الاستعلام نفسه
_counts = {}
COUNT_TTL = 30


def _option_count(queryset):
    sql, params = queryset.query.sql_with_params()
    key = (sql, tuple(map(str, params)))
    hit = _counts.get(key)
    if hit and time.monotonic() - hit[0] < COUNT_TTL:
        return hit[1]
    count = queryset[:THRESHOLD + 1].count()
    if len(_counts) > 500:
        _counts.clear()
    _counts[key] = (time.monotonic(), count)
    return count


def _token_key(token):
    return f"remote-select:{token}"


def _register(queryset):
    user, company = get_current_user(), get_current_company()
    if user is None or company is None:
        return None
    sql, params = queryset.query.sql_with_params()
    raw = f"{user.pk}:{company.pk}:{queryset.model._meta.label}:{sql}:{params}"
    token = hashlib.sha256((settings.SECRET_KEY + raw).encode()).hexdigest()[:32]
    key = _token_key(token)
    if cache.get(key) is None:
        cache.set(key, {"user": user.pk, "company": company.pk, "model": queryset.model._meta.label,
                        "query": pickle.dumps(queryset.query)}, settings.SESSION_COOKIE_AGE)
    return token


class RemoteSelect(forms.Select):
    """يعرض الخيار المختار فقط؛ باقي الخيارات تُجلب بالبحث من /api/options/."""

    def __init__(self, field, token, attrs=None):
        super().__init__(attrs)
        self.field, self.token = field, token

    def __deepcopy__(self, memo):
        obj = super().__deepcopy__(memo)
        obj.field, obj.token = self.field, self.token
        return obj

    def get_context(self, name, value, attrs):
        values = [v for v in (value if isinstance(value, (list, tuple)) else [value]) if v not in (None, "")]
        choices = [("", self.field.empty_label or "")] if self.field.empty_label is not None else []
        if values:
            try:
                selected = list(self.field.queryset.filter(pk__in=values))
            except (ValueError, TypeError):
                selected = []
            choices += [(obj.pk, self.field.label_from_instance(obj)) for obj in selected]
        self.choices = choices
        context = super().get_context(name, value, attrs)
        context["widget"]["attrs"]["data-remote"] = reverse("remote_options", args=[self.token])
        return context


def make_remote(field):
    """يحوّل حقل اختيار كبيراً إلى بحث من الخادم؛ يترك الحقول الصغيرة أو ذات التسميات المخصصة كما هي."""
    if not isinstance(field, forms.ModelChoiceField) or isinstance(field, forms.ModelMultipleChoiceField):
        return
    if type(field.widget) is not forms.Select or "label_from_instance" in field.__dict__:
        return
    if _option_count(field.queryset) <= THRESHOLD:
        return
    token = _register(field.queryset)
    if token:
        field.widget = RemoteSelect(field, token, attrs=field.widget.attrs)


def _search_fields(model):
    from core.registry import REGISTRY

    dt = REGISTRY.get(getattr(model, "doctype_slug", ""))
    names = {f.name for f in model._meta.concrete_fields}
    fields = [f for f in (dt.search_fields if dt else []) if f.split("__")[0] in names]
    return fields or [f for f in FALLBACK_SEARCH_FIELDS if f in names]


@login_required
def remote_options(request, token):
    from django.apps import apps

    from core.search import text_query

    entry = cache.get(_token_key(token))
    company = getattr(request, "company", None)
    if not entry or company is None or entry["user"] != request.user.pk or entry["company"] != company.pk:
        raise Http404
    model = apps.get_model(entry["model"])
    queryset = model._base_manager.all()
    queryset.query = pickle.loads(entry["query"])
    query = request.GET.get("q", "").strip()
    if query:
        fields = _search_fields(model)
        if fields:
            queryset = queryset.filter(text_query(fields, query))
    queryset = queryset.distinct()[:RESULTS + 1]
    rows = list(queryset)
    return JsonResponse({"results": [{"id": obj.pk, "text": str(obj)} for obj in rows[:RESULTS]],
                         "more": len(rows) > RESULTS})
