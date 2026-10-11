from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.display import detail_fields, display_value, field_label
from core.forms import build_form_class, build_formset_class, style_form
from core.models import DocStatus
from core.registry import get_doctype
from core.utils import client_ip, flt


def _error_text(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def dashboard(request):
    if not request.user.is_authenticated:
        from saas.views import landing

        return landing(request)
    membership = getattr(request, "membership", None)
    if membership is not None and not membership.allows_module("accounts"):
        # من لا يملك صلاحية المحاسبة لا يرى أرقام الأرباح والنقدية
        return render(request, "core/dashboard_limited.html", {"today": timezone.localdate()})
    from core.dashboard import dashboard_context

    return render(request, "core/dashboard.html", dashboard_context(request))


@login_required
def module_page(request, module):
    from core.registry import MODULES, menu

    section = next((m for m in menu() if m["key"] == module), None)
    if section is None:
        return redirect("dashboard")
    return render(request, "core/module.html", {"section": section, "module_label": MODULES[module]})


def filtered_queryset(request, dt):
    """القائمة بعد تطبيق البحث وفلاتر الحالة والنوع (تُستخدم في العرض والتصدير)."""
    from core.permissions import scope_queryset

    qs = scope_queryset(request, dt.model, dt.model.objects.all())
    query = request.GET.get("q", "").strip()
    if query and dt.search_fields:
        cond = Q()
        for f in dt.search_fields:
            cond |= Q(**{f"{f}__icontains": query})
        qs = qs.filter(cond)
    choice_filters = {}
    for field in dt.model._meta.concrete_fields:
        value = request.GET.get(field.name)
        if field.choices and field.name != "docstatus" and value in dict(field.choices):
            choice_filters[field.name] = value
    qs = qs.filter(**choice_filters)
    status = request.GET.get("status", "")
    if dt.is_submittable and status in ("0", "1", "2"):
        qs = qs.filter(docstatus=int(status))
    return qs, query, status, choice_filters


@login_required
def doc_list(request, slug):
    if slug == "account":
        return redirect("chart_of_accounts")
    dt = get_doctype(slug)
    qs, query, status, choice_filters = filtered_queryset(request, dt)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    rows = [
        {"obj": obj, "cells": [display_value(obj, f) for f in dt.list_fields]}
        for obj in page.object_list
    ]
    return render(request, "core/list.html", {
        "dt": dt,
        "headers": [field_label(dt.model, f) for f in dt.list_fields],
        "rows": rows,
        "page": page,
        "query": query,
        "status": status,
        "statuses": DocStatus.choices,
        "choice_filters": choice_filters,
        "filter_query": "&".join(f"{k}={v}" for k, v in choice_filters.items()),
        "filter_label": " / ".join(dict(dt.model._meta.get_field(k).choices)[v] for k, v in choice_filters.items()),
    })


@login_required
def doc_form(request, slug, pk=None):
    dt = get_doctype(slug)
    instance = get_object_or_404(_scoped(request, dt), pk=pk) if pk else None
    if instance is not None and dt.is_submittable and not instance.is_draft:
        messages.warning(request, "لا يمكن تعديل مستند معتمد أو ملغى.")
        return redirect(instance.get_absolute_url())

    FormClass = build_form_class(dt)
    is_return = getattr(instance, "is_return", False)
    if is_return:
        # في المرتجع تُعدّل الكميات والتاريخ والمستودع فقط؛ الباقي يُؤخذ من الفاتورة الأصلية
        from django.forms import modelform_factory

        from core.forms import formfield_callback

        FormClass = modelform_factory(dt.model, fields=["posting_date", "warehouse", "remarks"],
                                      formfield_callback=formfield_callback)
    FormSetClass = build_formset_class(dt) if dt.child else None
    initial = {}
    company = request.company
    if instance is None and company:
        if "currency" in FormClass.base_fields:
            initial["currency"] = company.currency_id
        if "tax_rate" in FormClass.base_fields:
            initial["tax_rate"] = company.default_tax_rate
        if "company" in FormClass.base_fields:
            initial["company"] = company
        if "account" in FormClass.base_fields and company.default_cash_account_id:
            initial["account"] = company.default_cash_account_id
        if "warehouse" in FormClass.base_fields:
            from stock.models import Warehouse

            initial["warehouse"] = Warehouse.objects.filter(disabled=False).values_list("id", flat=True).first()
    initial.update({k: v for k, v in request.GET.items() if k in FormClass.base_fields})

    from core.permissions import allowed_centers

    centers = allowed_centers(request)
    if centers is not None and "cost_center" in FormClass.base_fields:
        initial.setdefault("cost_center", sorted(centers)[0])
    form = FormClass(request.POST or None, instance=instance, initial=initial)
    if centers is not None and "cost_center" in form.fields:
        form.fields["cost_center"].queryset = form.fields["cost_center"].queryset.filter(id__in=centers)
        form.fields["cost_center"].required = True
    formset = FormSetClass(request.POST or None, instance=instance) if FormSetClass else None
    style_form(form)
    empty_form = None
    if formset is not None:
        # empty_form يُبنى من جديد عند كل استدعاء، لذا نحتفظ بنسخة واحدة منسقة للقالب
        empty_form = formset.empty_form
        for f in formset.forms + [empty_form]:
            style_form(f)

    if request.method == "POST" and request.POST.get("_submit") and dt.is_submittable:
        from core.permissions import require_submit

        try:
            require_submit(request)
        except ValidationError as exc:
            form.add_error(None, _error_text(exc))
    if request.method == "POST" and form.is_valid() and (formset is None or formset.is_valid()):
        try:
            with transaction.atomic():
                obj = form.save(commit=False)
                if hasattr(obj, "created_by_id") and not obj.pk:
                    obj.created_by = request.user
                obj.save()
                form.save_m2m()
                if formset is not None:
                    formset.instance = obj
                    formset.save()
                if dt.is_submittable:
                    obj.calculate_totals()
                    obj.save()
        except ValidationError as exc:
            form.add_error(None, _error_text(exc))
        except IntegrityError:
            form.add_error(None, "يوجد سجل آخر بنفس القيمة في هذه الشركة.")
        else:
            if dt.is_submittable and not request.POST.get("_submit"):
                messages.warning(request, f"تم حفظ {dt.label} كمسودة. لم تُرحّل القيود بعد — اضغط «اعتماد وترحيل» لإتمامها.")
            elif not dt.is_submittable:
                messages.success(request, f"تم حفظ {dt.label} بنجاح.")
            if request.POST.get("_submit") and dt.is_submittable:
                return _run_action(request, dt, obj, "submit")
            return redirect("doc_detail", slug=slug, pk=obj.pk)

    return render(request, "core/form.html", {
        "dt": dt, "form": form, "formset": formset, "empty_form": empty_form, "instance": instance, "is_return": is_return,
        "posts_to_ledger": getattr(dt.model, "posts_to_ledger", True),
    })


def _scoped(request, dt):
    from core.permissions import scope_queryset

    return scope_queryset(request, dt.model, dt.model.objects.all())


def _linked_entries(obj):
    from accounts.models import GLEntry
    from stock.models import StockLedgerEntry

    key = {"voucher_type": obj.doctype_slug, "voucher_id": obj.pk}
    gl = GLEntry.objects.filter(**key).select_related("account", "customer", "supplier", "cost_center")
    sle = StockLedgerEntry.objects.filter(**key).select_related("item", "warehouse")
    return gl, sle


def _child_table(dt, obj):
    if not dt.child:
        return [], []
    fields = list(dt.child.display_fields or dt.child.fields)
    if any(f.name == "amount" for f in dt.child.model._meta.fields) and "amount" not in fields:
        fields.append("amount")
    headers = [field_label(dt.child.model, f) for f in fields]
    related = getattr(obj, dt.child.model._meta.get_field(dt.child.fk_name).remote_field.related_name)
    rows = [[display_value(row, f) for f in fields] for row in related.all()]
    return headers, rows


@login_required
def doc_detail(request, slug, pk):
    dt = get_doctype(slug)
    obj = get_object_or_404(_scoped(request, dt), pk=pk)
    child_headers, child_rows = _child_table(dt, obj)
    exclude = set(dt.totals) | set(dt.detail_exclude) | {"name", "docstatus"}
    context = {
        "dt": dt, "obj": obj, "doc_currency": getattr(obj, "currency", None),
        "fields": detail_fields(obj, exclude=exclude),
        "totals": [(field_label(dt.model, f), getattr(obj, f)) for f in dt.totals],
        "child_headers": child_headers, "child_rows": child_rows,
    }
    if dt.is_submittable and not obj.is_draft:
        context["gl_entries"], context["stock_entries"] = _linked_entries(obj)
        debit = sum(g.debit for g in context["gl_entries"])
        credit = sum(g.credit for g in context["gl_entries"])
        context["gl_totals"] = {"debit": debit, "credit": credit, "balanced": debit == credit}
    if hasattr(obj, "payment_rows"):
        context["payments"] = obj.payment_rows()
    membership = getattr(request, "membership", None)
    if membership is not None and membership.can_manage:
        from django.db.models import Q

        from core.models import AuditLog

        label = obj._meta.label_lower
        context["audit_entries"] = (AuditLog.objects.filter(company=request.company)
                                    .filter(Q(model_label=label, object_id=str(obj.pk)) |
                                            Q(parent_label=label, parent_id=str(obj.pk)))
                                    .select_related("user")[:100])
    if slug in ("customer", "sales-invoice"):
        from messaging.models import MessagingSettings

        cfg = MessagingSettings.all_objects.filter(company=request.company).first()
        context["messaging_active"] = bool(cfg and cfg.active)
    return render(request, "core/detail.html", context)


@login_required
def doc_print(request, slug, pk):
    from core.hijri import format_hijri
    from core.models import PrintSettings, PrintTemplate
    from core.printing import print_context

    dt = get_doctype(slug)
    obj = get_object_or_404(_scoped(request, dt), pk=pk)
    ps = PrintSettings.for_company(request.company)
    template = request.GET.get("template")
    if template not in PrintTemplate.values:
        template = ps.template
    child_headers, child_rows = _child_table(dt, obj)
    qr = ""
    if slug == "sales-invoice" and not obj.is_draft:
        from core.zatca import invoice_qr_svg

        qr = invoice_qr_svg(obj)
    p = print_context(slug, obj, request.company, ps)
    exclude = set(dt.totals) | set(dt.detail_exclude) | {"name", "docstatus", "posting_date", "remarks", "company"}
    membership = getattr(request, "membership", None)
    posting_date = getattr(obj, "posting_date", None)
    return render(request, "core/print.html", {
        "qr": qr, "doc_currency": getattr(obj, "currency", None), "p": p, "ps": ps, "template": template,
        "templates": [(v, label.split(" — ")[0]) for v, label in PrintTemplate.choices],
        "can_customize": bool(membership and membership.can_manage),
        "hijri_date": format_hijri(posting_date) if posting_date else "",
        "generic_fields": detail_fields(obj, exclude=exclude) if p["kind"] == "generic" else [],
        "dt": dt, "obj": obj, "child_headers": child_headers, "child_rows": child_rows, "company": request.company,
        "totals": [(field_label(dt.model, f), getattr(obj, f)) for f in dt.totals if f != "outstanding_amount"],
    })


@login_required
def print_settings(request):
    from core.forms import PrintSettingsForm
    from core.models import PrintSettings

    obj = PrintSettings.for_company(request.company)
    form = PrintSettingsForm(request.POST or None, request.FILES or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ إعدادات الطباعة.")
        return redirect("print_settings")
    from selling.models import SalesInvoice

    sample = SalesInvoice.objects.filter(docstatus=1, is_pos=False).order_by("-id").first()
    return render(request, "settings/print.html", {"form": form, "obj": obj, "sample": sample})


def _run_action(request, dt, obj, action):
    from core.permissions import require_submit

    try:
        require_submit(request)
        if action == "submit":
            obj.submit()
            suffix = " وترحيل قيوده" if getattr(obj, "posts_to_ledger", True) else ""
            messages.success(request, f"تم اعتماد {dt.label} {obj}{suffix}.")
        elif action == "cancel":
            obj.cancel()
            messages.success(request, f"تم إلغاء {dt.label} {obj}.")
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect("doc_detail", slug=dt.slug, pk=obj.pk)


@login_required
@require_POST
def doc_action(request, slug, pk, action):
    dt = get_doctype(slug)
    obj = get_object_or_404(_scoped(request, dt), pk=pk)
    if action == "delete":
        if dt.is_submittable and not obj.is_draft:
            messages.error(request, "يمكن حذف المسودات فقط. ألغِ المستند بدلاً من ذلك.")
            return redirect(obj.get_absolute_url())
        try:
            obj.delete()
        except ProtectedError:
            messages.error(request, "لا يمكن الحذف لارتباط السجل بمستندات أخرى.")
            return redirect("doc_detail", slug=slug, pk=pk)
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect("doc_detail", slug=slug, pk=pk)
        messages.success(request, f"تم حذف {dt.label}.")
        return redirect("doc_list", slug=slug)
    if action in ("submit", "cancel") and dt.is_submittable:
        return _run_action(request, dt, obj, action)
    allowed = dict(list(getattr(obj, "available_actions", [])) + list(getattr(obj, "draft_actions", [])))
    if action in allowed:
        try:
            with transaction.atomic():
                if action == "reject":
                    from core.permissions import require_submit

                    require_submit(request)
                    result = obj.reject(request.user, request.POST.get("reason", "").strip())
                else:
                    result = getattr(obj, action)(request.user)
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect(obj.get_absolute_url())
        if result is not None and hasattr(result, "doctype_slug"):
            messages.info(request, f"تم إنشاء {result._meta.verbose_name} كمسودة من {obj}. راجع الكميات ثم اعتمد.")
            return redirect("doc_edit", slug=result.doctype_slug, pk=result.pk)
        messages.success(request, f"تم: {allowed[action]}.")
        return redirect(obj.get_absolute_url())
    if action == "return" and hasattr(obj, "make_return"):
        try:
            ret = obj.make_return(request.user)
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect(obj.get_absolute_url())
        messages.info(request, "تم إنشاء مسودة المرتجع بكل الكميات المتاحة. عدّل الكميات أو احذف الأسطر ثم اعتمد.")
        return redirect("doc_edit", slug=slug, pk=ret.pk)
    return redirect("doc_detail", slug=slug, pk=pk)


@login_required
def item_api(request, pk):
    from stock.models import Item

    item = get_object_or_404(Item, pk=pk)
    data = {
        "code": item.code, "name": item.name, "uom": str(item.uom), "uom_id": item.uom_id,
        "selling_rate": str(item.selling_rate), "buying_rate": str(item.buying_rate),
        # أسعار كل وحدة تحويل لتعبئة السعر عند اختيار الوحدة في السطر
        "uoms": {str(u["uom"]): {"name": u["name"], "factor": str(u["factor"]), "selling_rate": str(u["selling_rate"]),
                                 "buying_rate": str(u["buying_rate"])} for u in item.uom_options()},
    }
    kind = request.GET.get("kind")
    if kind in ("selling", "buying"):
        data["priced"] = _priced(request, item, kind)
    return JsonResponse(data)


def _priced(request, item, kind):
    """سعر السطر بقائمة الأسعار وقواعد التسعير للطرف والكمية والتاريخ (كما سيحسبه الخادم عند الحفظ)."""
    from django.utils.dateparse import parse_date

    from buying.models import Supplier
    from pricing.engine import price_line, resolve_price_list
    from pricing.models import PriceList
    from selling.models import Customer

    def pick(model, key):
        value = request.GET.get(key, "")
        return model.objects.filter(pk=value).first() if value.isdigit() else None

    party = pick(Customer, "customer") if kind == "selling" else pick(Supplier, "supplier")
    price_list = resolve_price_list(kind, party, pick(PriceList, "price_list"))
    uom = request.GET.get("uom", "")
    try:
        qty = flt(request.GET.get("qty") or 1, 3)
        result = price_line(item, qty, int(uom) if uom.isdigit() else None, kind, party, price_list,
                            parse_date(request.GET.get("date") or "") or timezone.localdate(),
                            request.GET.get("coupon", ""), "backoffice",
                            ignore_rules=request.GET.get("ignore_rules") in ("1", "true", "on"))
    except (ValidationError, ArithmeticError, ValueError):
        return None
    return {"rate": str(result["rate"]), "price_list_rate": str(result["price_list_rate"]),
            "discount_percentage": str(result["discount_percentage"]),
            "rule": result["rule"].name if result["rule"] else "",
            "free_item": result["gift_rule"].free_item.name if result["gift_rule"] else "",
            "free_qty": str(result["free_qty"])}



@login_required
def notifications(request):
    from core.models import Notification

    items = Notification.objects.filter(user=request.user)
    if request.method == "POST":
        items.filter(is_read=False).update(is_read=True)
        return redirect("notifications")
    page = Paginator(items, 30).get_page(request.GET.get("page"))
    return render(request, "core/notifications.html", {"page": page})


@login_required
def notification_open(request, pk):
    from core.models import Notification

    item = get_object_or_404(Notification, pk=pk, user=request.user)
    item.is_read = True
    item.save(update_fields=["is_read"])
    return redirect(item.url or "notifications")


def email_settings_page(request, obj, platform):
    from core.forms import EmailSettingsForm
    from core.mail import active_config, send_test

    form = EmailSettingsForm(request.POST or None, instance=obj, platform=platform)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "test":
            to = request.POST.get("test_to", "").strip() or request.user.email
            if not to:
                messages.error(request, "أدخل بريداً لاستلام رسالة الاختبار.")
            else:
                ok, message = send_test(obj, to)
                (messages.success if ok else messages.error)(request, message)
            return redirect(request.path)
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ إعدادات البريد." + (" الإرسال مفعّل الآن." if obj.enabled else
                                                                   " الإرسال غير مفعّل بعد."))
            return redirect(request.path)
    company = None if platform else request.company
    return render(request, "settings/email.html", {
        "form": form, "obj": obj, "platform": platform, "active": active_config(company),
        "platform_settings": None if platform else type(obj).objects.filter(company__isnull=True).first(),
    })


@login_required
def company_email_settings(request):
    from core.models import EmailSettings

    return email_settings_page(request, EmailSettings.for_company(request.company), platform=False)


@login_required
def platform_email_settings(request):
    from core.models import EmailSettings

    if not request.user.is_superuser:
        return redirect("dashboard")
    return email_settings_page(request, EmailSettings.platform(), platform=True)


class ProfileForm(forms.Form):
    first_name = forms.CharField(label="الاسم الأول", max_length=150, required=False)
    last_name = forms.CharField(label="اسم العائلة", max_length=150, required=False)
    email = forms.EmailField(label="البريد الإلكتروني", required=False,
                             help_text="تصلك عليه التنبيهات (إن فُعّل البريد) ورابط استعادة كلمة المرور")


@login_required
def profile(request):
    from core.forms import style_form

    user = request.user
    form = style_form(ProfileForm(request.POST or None, initial={
        "first_name": user.first_name, "last_name": user.last_name, "email": user.email}))
    if request.method == "POST" and form.is_valid():
        for field in ("first_name", "last_name", "email"):
            setattr(user, field, form.cleaned_data[field])
        user.save(update_fields=["first_name", "last_name", "email"])
        messages.success(request, "تم حفظ بياناتك.")
        return redirect("profile")
    return render(request, "settings/profile.html", {"form": form})


def healthz(request):
    """فحص صحة الخدمة وقاعدة البيانات (لـ Docker والمراقبة)."""
    from django.db import connection
    from django.http import HttpResponse

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return HttpResponse("db-error", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


@login_required
def api_keys(request):
    """إدارة مفاتيح الواجهة البرمجية (للمالك والمدير)."""
    from core.models import ApiKey, Membership, Role

    company = request.company
    members = Membership.objects.filter(company=company, is_active=True).exclude(
        role__in=[Role.EMPLOYEE, Role.CASHIER]).select_related("user")
    new_key = None
    if request.method == "POST":
        if request.POST.get("action") == "revoke":
            ApiKey.objects.filter(company=company, pk=request.POST.get("key")).update(revoked=True)
            messages.success(request, "تم إلغاء المفتاح؛ أي نظام يستخدمه سيُرفض فوراً.")
            return redirect("api_keys")
        name = request.POST.get("name", "").strip()
        member = members.filter(pk=request.POST.get("member")).first()
        if not name or member is None:
            messages.error(request, "أدخل اسماً للمفتاح واختر المستخدم.")
        else:
            _, new_key = ApiKey.issue(company, member.user, name)
            messages.success(request, "تم إنشاء المفتاح. انسخه الآن؛ لن يظهر مرة أخرى.")
    return render(request, "settings/api_keys.html", {
        "keys": ApiKey.objects.filter(company=company).select_related("user"), "members": members,
        "new_key": new_key, "base_url": request.build_absolute_uri("/api/v1/"),
    })


@login_required
def api_docs(request):
    from core.api import doctype_schema
    from core.registry import REGISTRY

    schemas = sorted((doctype_schema(dt) for dt in REGISTRY.values()), key=lambda s: (s["module"], s["slug"]))
    return render(request, "settings/api_docs.html", {"schemas": schemas,
                                                      "base_url": request.build_absolute_uri("/api/v1/")})


@login_required
def audit_log(request):
    """سجل التعديلات للشركة (للمدير): من غيّر ماذا ومتى."""
    from django.contrib.auth.models import User
    from django.core.paginator import Paginator

    from core.models import AuditAction, AuditLog

    entries = AuditLog.objects.filter(company=request.company).select_related("user")
    selected = {k: request.GET.get(k, "") for k in ("user", "action", "model", "q", "from_date", "to_date")}
    if selected["user"]:
        entries = entries.filter(user_id=selected["user"])
    if selected["action"]:
        entries = entries.filter(action=selected["action"])
    if selected["model"]:
        entries = entries.filter(model_label=selected["model"])
    if selected["q"]:
        entries = entries.filter(object_repr__icontains=selected["q"])
    if selected["from_date"]:
        entries = entries.filter(created_at__date__gte=selected["from_date"])
    if selected["to_date"]:
        entries = entries.filter(created_at__date__lte=selected["to_date"])
    models_used = (AuditLog.objects.filter(company=request.company).values_list("model_label", "model_name")
                   .distinct().order_by("model_name"))
    users = User.objects.filter(memberships__company=request.company).distinct().order_by("username")
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "core/audit_log.html", {
        "page": page, "selected": selected, "actions": AuditAction.choices, "models_used": sorted(set(models_used),
                                                                                                key=lambda m: m[1]),
        "users": users, "query": query.urlencode(),
    })


@login_required
def company_export(request):
    """تصدير كامل بيانات الشركة (لمالك الحساب فقط)."""
    from core.company_export import company_querysets, export_filename

    membership = request.membership
    allowed = membership.role == "owner" and membership.pk is not None  # ليس لوضع الدعم الفني
    if request.method == "POST" and allowed:
        import tempfile

        from django.http import FileResponse

        from core.audit import log_event
        from core.company_export import write_export
        from core.notify import notify_managers

        company = request.company
        tmp = tempfile.TemporaryFile()
        manifest = write_export(company, tmp, request.user)
        tmp.seek(0)
        who = request.user.get_full_name() or request.user.username
        log_event(company, request.user, "export", company,
                  {"عدد الجداول": [None, len(manifest)], "عدد السجلات": [None, sum(t["count"] for t in manifest)]},
                  client_ip(request))
        notify_managers(company, f"صدّر {who} نسخة كاملة من بيانات الشركة", "/settings/audit/?action=export")
        return FileResponse(tmp, as_attachment=True, filename=export_filename(company),
                            content_type="application/zip")
    tables = [(str(model._meta.verbose_name_plural), qs.count()) for model, qs in company_querysets(request.company)]
    tables = [t for t in tables if t[1]]
    return render(request, "core/company_export.html", {
        "allowed": allowed, "tables": tables, "total": sum(c for _n, c in tables)})
