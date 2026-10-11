"""الواجهة البرمجية REST (JSON) — /api/v1/

المصادقة: Authorization: Bearer snr_xxx (مفتاح من «الإعدادات ← الواجهة البرمجية»).
كل طلب يعمل بصلاحيات المستخدم المرتبط بالمفتاح (الوحدات، الاعتماد، الفروع)، والتحقق من البيانات
يمر بنفس نماذج الواجهة فتنطبق كل قواعد النظام.
"""
import datetime
import json
from decimal import Decimal
from functools import wraps

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, models, transaction
from django.db.models import ProtectedError, Q, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import ApiKey, DocStatus, Membership
from core.registry import REGISTRY
from core.tenancy import _current_user, reset_current_company, set_current_company, set_current_user

RATE_LIMIT_PER_MINUTE = 120
HIDDEN = {"company", "created_by", "password_encrypted", "secret_encrypted", "private_key"}


def error(message, status=400, code="invalid", **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status,
                        json_dumps_params={"ensure_ascii": False})


def ok(data, status=200):
    return JsonResponse(data, status=status, safe=not isinstance(data, list),
                        json_dumps_params={"ensure_ascii": False})


def api_view(view):
    """مصادقة المفتاح، وتحديد الشركة، والحد من معدل الطلبات."""
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        raw = header[7:].strip() if header.lower().startswith("bearer ") else request.META.get("HTTP_X_API_KEY", "")
        key = ApiKey.authenticate(raw)
        if key is None:
            return error("مفتاح API غير صالح أو ملغى.", 401, "unauthorized")
        membership = Membership.objects.filter(user=key.user, company=key.company, is_active=True).first()
        if membership is None or not key.user.is_active or membership.role in ("employee", "cashier"):
            return error("المستخدم المرتبط بالمفتاح غير نشط في الشركة أو لا يملك صلاحية الواجهة البرمجية.", 403,
                         "forbidden")
        subscription = getattr(key.company, "subscription", None)
        if subscription is None or not subscription.is_valid:
            return error("اشتراك الشركة منتهٍ.", 402, "subscription_expired")
        bucket = f"api-rate:{key.pk}:{int(timezone.now().timestamp() // 60)}"
        if cache.get_or_set(bucket, 0, 70) >= RATE_LIMIT_PER_MINUTE:
            return error(f"تجاوزت الحد ({RATE_LIMIT_PER_MINUTE} طلب في الدقيقة).", 429, "rate_limited")
        try:
            cache.incr(bucket)
        except ValueError:
            pass
        if not key.last_used_at or (timezone.now() - key.last_used_at).total_seconds() > 60:
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        request.user, request.membership, request.company, request.api_key = key.user, membership, key.company, key
        token = set_current_company(key.company)
        user_token = set_current_user(key.user)
        try:
            return view(request, *args, **kwargs)
        finally:
            reset_current_company(token)
            _current_user.reset(user_token)
    return wrapper


# ---------------- التحويل إلى JSON ----------------

def _value(obj, field):
    value = getattr(obj, field.attname)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    return value


def serialize(obj, child=None):
    data = {"id": obj.pk}
    for field in obj._meta.concrete_fields:
        if field.name in HIDDEN or field.name == "id":
            continue
        data[field.name] = _value(obj, field)
        if field.is_relation and data[field.name] is not None:
            data[f"{field.name}_display"] = str(getattr(obj, field.name))
        elif field.choices:
            data[f"{field.name}_display"] = getattr(obj, f"get_{field.name}_display")()
    if child is not None:
        related = child.model._meta.get_field(child.fk_name).remote_field.related_name
        data[related] = [serialize(row) for row in getattr(obj, related).all()]
    return data


def field_schema(model, names):
    result = []
    for name in names:
        f = model._meta.get_field(name)
        kind = ("link" if f.is_relation else "decimal" if isinstance(f, models.DecimalField)
                else "date" if isinstance(f, models.DateField) else "boolean" if isinstance(f, models.BooleanField)
                else "integer" if isinstance(f, models.IntegerField) else "text")
        entry = {"name": name, "label": str(f.verbose_name), "type": kind,
                 "required": not (f.blank or f.has_default() or f.null)}
        if f.is_relation:
            entry["link_to"] = f.related_model._meta.verbose_name
        if f.choices:
            entry["choices"] = [{"value": v, "label": str(label)} for v, label in f.choices]
        result.append(entry)
    return result


def doctype_schema(dt):
    from core.forms import build_form_class

    form_fields = list(build_form_class(dt).base_fields)
    schema = {"slug": dt.slug, "label": str(dt.label), "label_plural": str(dt.label_plural), "module": dt.module,
              "submittable": dt.is_submittable, "fields": field_schema(dt.model, form_fields)}
    if dt.child:
        related = dt.child.model._meta.get_field(dt.child.fk_name).remote_field.related_name
        schema["child"] = {"key": related, "label": str(dt.child.label),
                           "fields": field_schema(dt.child.model, list(dt.child.fields))}
    return schema


# ---------------- الصلاحيات ----------------

def _doctype(request, slug):
    from core.permissions import modules_for_path

    dt = REGISTRY.get(slug)
    if dt is None:
        return None, error("نوع المستند غير موجود.", 404, "not_found")
    modules = modules_for_path(f"/app/{slug}/")
    if modules and not any(request.membership.allows_module(m) for m in modules):
        return None, error("ليست لديك صلاحية على هذه الوحدة.", 403, "forbidden")
    return dt, None


def _queryset(request, dt):
    from core.permissions import scope_queryset

    return scope_queryset(request, dt.model, dt.model.objects.all())


# ---------------- الإنشاء والتعديل عبر نماذج الواجهة ----------------

def _form_value(value):
    if value is True:
        return "on"
    if value is False or value is None:
        return ""
    return str(value)


def _defaults(model, names):
    """القيم الافتراضية للحقول (كما تظهر معبأة في الشاشات) لمن لم يرسلها في JSON."""
    result = {}
    for name in names:
        field = model._meta.get_field(name)
        if field.has_default():
            value = field.get_default()
            result[name] = value.pk if hasattr(value, "pk") else value
    return result


def _save(request, dt, payload, instance=None):
    """يُنشئ أو يعدّل مستنداً بنفس نموذج الواجهة؛ يعيد (المستند، الأخطاء)."""
    from django.forms.models import model_to_dict

    from core.forms import build_form_class, build_formset_class
    from core.permissions import allowed_centers

    FormClass = build_form_class(dt)
    data = {}
    if instance is not None:
        data.update({k: v for k, v in model_to_dict(instance, fields=list(FormClass.base_fields)).items()})
    else:
        data.update(_defaults(dt.model, FormClass.base_fields))
        centers = allowed_centers(request)
        if centers and "cost_center" in FormClass.base_fields and "cost_center" not in payload:
            data["cost_center"] = sorted(centers)[0]
    for name in FormClass.base_fields:
        if name in payload:
            data[name] = payload[name]
    form_data = {k: _form_value(v.pk if hasattr(v, "pk") else v) for k, v in data.items()}
    form = FormClass(form_data, instance=instance)
    centers = allowed_centers(request)
    if centers is not None and "cost_center" in form.fields:
        form.fields["cost_center"].queryset = form.fields["cost_center"].queryset.filter(id__in=centers)
    formset = None
    rows = None
    if dt.child:
        related = dt.child.model._meta.get_field(dt.child.fk_name).remote_field.related_name
        rows = payload.get(related)
        if rows is not None:
            if not isinstance(rows, list):
                return None, {related: ["يجب أن تكون قائمة."]}
            FormSet = build_formset_class(dt)
            prefix = FormSet.get_default_prefix()
            fs_data = {f"{prefix}-TOTAL_FORMS": str(len(rows)), f"{prefix}-INITIAL_FORMS": "0",
                       f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000"}
            child_defaults = _defaults(dt.child.model, dt.child.fields)
            for i, row in enumerate(rows):
                for key in dt.child.fields:
                    value = row[key] if key in row else child_defaults.get(key)
                    if value is not None:
                        fs_data[f"{prefix}-{i}-{key}"] = _form_value(value)
            formset = FormSet(fs_data, instance=dt.model(), prefix=prefix)
        elif instance is None and dt.child.min_rows:
            return None, {related: ["مطلوب سطر واحد على الأقل."]}
    errors = {}
    if not form.is_valid():
        errors.update({k: [str(m) for m in v] for k, v in form.errors.items()})
    if formset is not None and not formset.is_valid():
        for i, row_errors in enumerate(formset.errors):
            for k, v in row_errors.items():
                errors[f"{related}[{i}].{k}"] = [str(m) for m in v]
        for m in formset.non_form_errors():
            errors.setdefault(related, []).append(str(m))
    if errors:
        return None, errors
    try:
        with transaction.atomic():
            obj = form.save(commit=False)
            if hasattr(obj, "created_by_id") and not obj.pk:
                obj.created_by = request.user
            obj.save()
            form.save_m2m()
            if formset is not None:
                getattr(obj, related).all().delete()
                formset.instance = obj
                formset.save()
            if dt.is_submittable:
                obj.calculate_totals()
                obj.save()
    except ValidationError as exc:
        return None, {"__all__": list(exc.messages)}
    except IntegrityError:
        return None, {"__all__": ["يوجد سجل آخر بنفس القيمة في هذه الشركة."]}
    return obj, None


def _run(request, obj, action):
    from core.permissions import require_submit

    with transaction.atomic():
        if action in ("submit", "cancel"):
            require_submit(request)
            getattr(obj, action)()
            return None
        allowed = dict(list(getattr(obj, "available_actions", [])) + list(getattr(obj, "draft_actions", [])))
        if action not in allowed:
            raise ValidationError(f"الإجراء «{action}» غير متاح لهذا المستند الآن. المتاح: "
                                  + ("، ".join(allowed) or "لا شيء"))
        if action == "reject":
            require_submit(request)
            return obj.reject(request.user)
        return getattr(obj, action)(request.user)


def _json(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("جسم الطلب ليس JSON صالحاً.")
    if not isinstance(payload, dict):
        raise ValidationError("جسم الطلب يجب أن يكون كائن JSON.")
    return payload


# ---------------- نقاط النهاية ----------------

@api_view
def index(request):
    doctypes = [doctype_schema(dt) for dt in REGISTRY.values()]
    from core.permissions import modules_for_path

    visible = []
    for schema in doctypes:
        modules = modules_for_path(f"/app/{schema['slug']}/")
        if not modules or any(request.membership.allows_module(m) for m in modules):
            visible.append(schema)
    return ok({"company": request.company.name, "user": request.user.username, "version": "v1",
               "doctypes": visible})


@api_view
def collection(request, slug):
    dt, err = _doctype(request, slug)
    if err:
        return err
    if request.method == "GET":
        qs = _queryset(request, dt)
        query = request.GET.get("q", "").strip()
        if query and dt.search_fields:
            cond = Q()
            for f in dt.search_fields:
                cond |= Q(**{f"{f}__icontains": query})
            qs = qs.filter(cond)
        names = {f.name: f for f in dt.model._meta.concrete_fields}
        for key, value in request.GET.items():
            base = key.split("__")[0]
            if base in names and base not in HIDDEN and key.split("__")[-1] in (base, "gte", "lte", "in"):
                qs = qs.filter(**{key: value.split(",") if key.endswith("__in") else value})
        if "modified_since" in request.GET and "modified_at" in names:
            qs = qs.filter(modified_at__gte=request.GET["modified_since"])
        try:
            size = max(1, min(int(request.GET.get("page_size", 50)), 100))
            page = Paginator(qs, size).get_page(request.GET.get("page"))
        except (ValueError, ValidationError):
            return error("معاملات تصفية غير صحيحة.")
        return ok({"count": page.paginator.count, "page": page.number, "pages": page.paginator.num_pages,
                   "results": [serialize(o) for o in page.object_list]})
    if request.method == "POST":
        try:
            payload = _json(request)
        except ValidationError as exc:
            return error(exc.messages[0])
        obj, errors = _save(request, dt, payload)
        if errors:
            return error("تعذر الحفظ؛ راجع الحقول.", 400, "validation_error", fields=errors)
        if payload.get("submit") and dt.is_submittable:
            try:
                _run(request, obj, "submit")
            except ValidationError as exc:
                return error(" ".join(exc.messages), 400, "submit_failed", id=obj.pk,
                             document=serialize(obj, dt.child))
            obj.refresh_from_db()
        return ok(serialize(obj, dt.child), 201)
    return error("الطريقة غير مدعومة.", 405, "method_not_allowed")


@api_view
def item(request, slug, pk):
    dt, err = _doctype(request, slug)
    if err:
        return err
    obj = _queryset(request, dt).filter(pk=pk).first()
    if obj is None:
        return error("السجل غير موجود.", 404, "not_found")
    if request.method == "GET":
        return ok(serialize(obj, dt.child))
    editable = not dt.is_submittable or obj.is_draft
    if request.method in ("PATCH", "PUT"):
        if not editable:
            return error("لا يمكن تعديل مستند معتمد أو ملغى.", 409, "not_editable")
        try:
            payload = _json(request)
        except ValidationError as exc:
            return error(exc.messages[0])
        obj, errors = _save(request, dt, payload, instance=obj)
        if errors:
            return error("تعذر الحفظ؛ راجع الحقول.", 400, "validation_error", fields=errors)
        return ok(serialize(obj, dt.child))
    if request.method == "DELETE":
        if not editable:
            return error("يمكن حذف المسودات فقط؛ ألغِ المستند بدلاً من ذلك.", 409, "not_editable")
        try:
            obj.delete()
        except (ProtectedError, ValidationError):
            return error("لا يمكن الحذف لارتباط السجل بمستندات أخرى.", 409, "protected")
        return ok({"deleted": True})
    return error("الطريقة غير مدعومة.", 405, "method_not_allowed")


@api_view
def action(request, slug, pk, name):
    if request.method != "POST":
        return error("استخدم POST.", 405, "method_not_allowed")
    dt, err = _doctype(request, slug)
    if err:
        return err
    obj = _queryset(request, dt).filter(pk=pk).first()
    if obj is None:
        return error("السجل غير موجود.", 404, "not_found")
    try:
        result = _run(request, obj, name)
    except ValidationError as exc:
        return error(" ".join(exc.messages), 400, "action_failed")
    obj.refresh_from_db()
    data = {"document": serialize(obj, dt.child)}
    if result is not None and hasattr(result, "doctype_slug"):
        data["created"] = {"doctype": result.doctype_slug, "id": result.pk, "name": getattr(result, "name", "")}
    return ok(data)


@api_view
def stock_balance(request):
    if not request.membership.allows_module("stock") and not request.membership.allows_module("selling"):
        return error("ليست لديك صلاحية على المخزون.", 403, "forbidden")
    from stock.models import Bin

    qs = Bin.objects.select_related("item", "warehouse").order_by("item__code")
    if request.GET.get("warehouse"):
        qs = qs.filter(warehouse_id=request.GET["warehouse"])
    if request.GET.get("item"):
        qs = qs.filter(item_id=request.GET["item"])
    return ok({"results": [{"item": b.item_id, "item_code": b.item.code, "item_name": b.item.name,
                            "warehouse": b.warehouse_id, "warehouse_name": b.warehouse.name,
                            "qty": str(b.actual_qty), "valuation_rate": str(b.valuation_rate),
                            "stock_value": str(b.stock_value)} for b in qs]})


@api_view
def account_balances(request):
    if not request.membership.allows_module("accounts"):
        return error("ليست لديك صلاحية على المحاسبة.", 403, "forbidden")
    from accounts.models import Account, GLEntry

    as_of = request.GET.get("as_of") or timezone.localdate().isoformat()
    sums = {r["account"]: (r["d"] or 0, r["c"] or 0) for r in GLEntry.objects.filter(
        is_cancelled=False, posting_date__lte=as_of).values("account").annotate(d=Sum("debit"), c=Sum("credit"))}
    rows = []
    for account in Account.objects.filter(is_group=False).order_by("number"):
        d, c = sums.get(account.id, (0, 0))
        if d or c:
            rows.append({"account": account.id, "number": account.number, "name": account.name,
                         "root_type": account.root_type, "debit": str(d), "credit": str(c), "balance": str(d - c)})
    return ok({"as_of": as_of, "results": rows})


# ---------------- الموازين (للبرنامج الوسيط في الفرع) ----------------

@api_view
def scales_sync(request):
    """أسعار الأصناف الموزونة لكل ميزان. يدعم If-None-Match: إن لم تتغير الأسعار يُعاد 304 بلا محتوى."""
    from django.http import HttpResponseNotModified

    from stock.scale import scales_payload

    if request.method != "GET":
        return error("الطريقة غير مدعومة.", 405, "method_not_allowed")
    if not request.membership.allows_module("stock"):
        return error("المستخدم لا يملك صلاحية المخزون.", 403, "forbidden")
    payload = scales_payload(request.company)
    etag = f'"{payload["version"]}"'
    if request.META.get("HTTP_IF_NONE_MATCH") == etag:
        response = HttpResponseNotModified()
    else:
        response = ok(payload)
    response["ETag"] = etag
    return response


@api_view
def scale_report(request, pk):
    """البرنامج الوسيط يبلّغ بنتيجة إرسال الأسعار للميزان."""
    from stock.models import Scale

    if request.method != "POST":
        return error("الطريقة غير مدعومة.", 405, "method_not_allowed")
    if not request.membership.allows_module("stock"):
        return error("المستخدم لا يملك صلاحية المخزون.", 403, "forbidden")
    scale = Scale.objects.filter(pk=pk).first()
    if scale is None:
        return error("الميزان غير موجود.", 404, "not_found")
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return error("بيانات غير صالحة.")
    was_ok = scale.last_sync_ok
    scale.last_sync_at = timezone.now()
    scale.last_sync_ok = bool(data.get("ok"))
    scale.last_sync_message = str(data.get("message") or ("تمت المزامنة" if scale.last_sync_ok else "فشلت"))[:300]
    if scale.last_sync_ok:
        scale.last_version = str(data.get("version") or "")[:64]
        try:
            scale.last_plu_count = max(0, int(data.get("plu_count") or 0))
        except (TypeError, ValueError):
            pass
    scale.save(update_fields=["last_sync_at", "last_sync_ok", "last_sync_message", "last_version", "last_plu_count"])
    if not scale.last_sync_ok and was_ok is not False:  # تنبيه عند أول فشل فقط لا مع كل محاولة
        from core.notify import notify_managers

        try:
            notify_managers(request.company, f"تعذر تحديث أسعار الميزان «{scale.name}»", url="/stock/scales/",
                            body=scale.last_sync_message)
        except Exception:  # noqa: BLE001 — التنبيه لا يُفشل البلاغ
            pass
    return ok({"ok": True})
