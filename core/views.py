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
from core.models import Attachment, DocStatus
from core.permissions import sees_costs
from core.registry import get_doctype
from core.utils import client_ip, flt


def _error_text(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def dashboard(request):
    if not request.user.is_authenticated:
        from saas.views import landing

        return landing(request)
    membership = getattr(request, "membership", None)
    if membership is not None and (not membership.allows_module("accounts") or not membership.may("costs")):
        # من لا يملك صلاحية المحاسبة لا يرى أرقام الأرباح والنقدية
        return render(request, "core/dashboard_limited.html", {"today": timezone.localdate()})
    from core.onboarding import onboarding_context

    if request.company.is_nonprofit:
        from core.dashboard import nonprofit_dashboard_context

        return render(request, "core/dashboard_nonprofit.html",
                      {**nonprofit_dashboard_context(request), "onboarding": onboarding_context(request)})
    from core.dashboard import dashboard_context

    return render(request, "core/dashboard.html", {**dashboard_context(request), "onboarding": onboarding_context(request)})


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
    # أعمدة الربط (الصنف، العميل، المجموعة...) تُجلب في الاستعلام نفسه بدل استعلام لكل سطر
    related = [f for f in dt.list_fields
               if (field := dt.model._meta.get_field(f)).many_to_one or field.one_to_one]
    if related:
        qs = qs.select_related(*related)
    query = request.GET.get("q", "").strip()
    if query and dt.search_fields:
        from core.search import text_query

        qs = qs.filter(text_query(dt.search_fields, query)).distinct()
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
        from django.urls import reverse
        from django.utils.http import urlencode

        query = request.GET.get("q", "").strip()
        return redirect(reverse("chart_of_accounts") + (f"?{urlencode({'q': query})}" if query else ""))
    dt = get_doctype(slug)
    qs, query, status, choice_filters = filtered_queryset(request, dt)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    rows = [
        {"obj": obj, "cells": [display_value(obj, f) for f in dt.list_fields]}
        for obj in page.object_list
    ]
    return render(request, "core/list.html", {
        "dt": dt,
        "can_disable": any(f.name == "disabled" for f in dt.model._meta.fields),
        "can_bulk": not dt.read_only and (not request.membership or request.membership.doctype_access(slug) == "write"),
        "can_delete": not request.membership or request.membership.may("delete"),
        "headers": [field_label(dt.model, f) for f in dt.list_fields],
        "rows": rows,
        "page": page,
        "query": query,
        "status": status,
        "statuses": DocStatus.choices,
        "choice_filters": choice_filters,
        "filter_query": "&".join(f"{k}={v}" for k, v in choice_filters.items()),
        "filter_label": " / ".join(dict(dt.model._meta.get_field(k).choices)[v] for k, v in choice_filters.items()),
        "type_title": dt.type_titles.get(choice_filters.get(dt.type_field)) if dt.type_field else None,
    })


@login_required
@require_POST
def doc_bulk(request, slug):
    from core.permissions import require_action
    dt = get_doctype(slug)
    try:
        if dt.read_only or (request.membership and request.membership.doctype_access(slug) != "write"):
            raise ValidationError("هذه القائمة للقراءة فقط.")
        ids = set(request.POST.getlist("selected"))
        if not ids or len(ids) > 100 or any(not value.isdigit() for value in ids):
            raise ValidationError("حدد من سجل واحد إلى 100 سجل في الصفحة الحالية.")
        action = request.POST.get("action")
        if action not in ("delete", "disable", "enable"):
            raise ValidationError("إجراء غير صالح.")
        require_action(request, "delete" if action == "delete" else "write")
        with transaction.atomic():
            objects = list(_scoped(request, dt).select_for_update().filter(pk__in=ids))
            if len(objects) != len(ids):
                raise ValidationError("بعض السجلات غير متاحة لك. لم يُنفذ الإجراء.")
            for obj in objects:
                if action == "delete":
                    if dt.is_submittable and not obj.is_draft:
                        raise ValidationError("يمكن حذف المسودات فقط؛ لم يُحذف أي سجل.")
                    object_id = obj.pk
                    obj.delete()
                    Attachment.objects.filter(doctype=slug, object_id=object_id).delete()
                else:
                    if not any(f.name == "disabled" for f in dt.model._meta.fields):
                        raise ValidationError("الإيقاف غير متاح لهذه القائمة.")
                    obj.disabled = action == "disable"
                    obj.save(update_fields=["disabled"])
    except (ValidationError, ProtectedError) as exc:
        messages.error(request, _error_text(exc) if isinstance(exc, ValidationError)
                       else "لا يمكن حذف سجلات مرتبطة بمستندات؛ لم يُحذف أي سجل.")
    else:
        messages.success(request, f"تم تنفيذ الإجراء على {len(objects)} سجل.")
    return redirect("doc_list", slug=slug)


@login_required
def doc_form(request, slug, pk=None):
    dt = get_doctype(slug)
    if dt.read_only:
        messages.info(request, f"{dt.label_plural} يُنشئها النظام تلقائياً ولا تُعدَّل يدوياً.")
        return redirect("doc_detail", slug=slug, pk=pk) if pk else redirect("doc_list", slug=slug)
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
    # نوع محدد من الرابط (سند قبض / سند صرف): يُخفى اختياره ويصبح عنوان النموذج
    type_value = getattr(instance, dt.type_field, None) if instance else request.GET.get(dt.type_field)
    type_title = dt.type_titles.get(type_value) if dt.type_field else None

    from core.permissions import allowed_centers

    centers = allowed_centers(request)
    if centers is not None and "cost_center" in FormClass.base_fields:
        initial.setdefault("cost_center", sorted(centers)[0])
    form = FormClass(request.POST or None, instance=instance, initial=initial)
    if "represents_company" in form.fields:
        # الشركات الشقيقة: فقط ما يديره المستخدم نفسه (لا تُعرض شركات المشتركين الآخرين)
        from core.intercompany import managed_companies

        form.fields["represents_company"].queryset = managed_companies(request.user).exclude(pk=company.pk if company else None)
    if centers is not None and "cost_center" in form.fields:
        form.fields["cost_center"].queryset = form.fields["cost_center"].queryset.filter(id__in=centers)
        form.fields["cost_center"].required = True
    formset = FormSetClass(request.POST or None, instance=instance) if FormSetClass else None
    if type_title:
        from django.forms import HiddenInput

        form.fields[dt.type_field].widget = HiddenInput()
    hidden_fields = nonprofit_only_fields(company)
    for name in hidden_fields:
        form.fields.pop(name, None)
    style_form(form)
    empty_form = None
    if formset is not None:
        # empty_form يُبنى من جديد عند كل استدعاء، لذا نحتفظ بنسخة واحدة منسقة للقالب
        empty_form = formset.empty_form
        for f in formset.forms + [empty_form]:
            for name in hidden_fields:
                f.fields.pop(name, None)
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
                    if instance is not None:
                        from core.approvals import reset

                        reset(obj)
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

    advanced = _advanced_fields(dt, form, company)
    return render(request, "core/form.html", {
        "dt": dt, "form": form, "formset": formset, "empty_form": empty_form, "instance": instance, "is_return": is_return,
        "posts_to_ledger": getattr(dt.model, "posts_to_ledger", True), "type_title": type_title,
        "advanced_names": [bf.name for bf in advanced], "advanced_fields": advanced,
        "advanced_open": _advanced_open(form, advanced, company),
    })


def _advanced_fields(dt, form, company):
    """الحقول التي تُطوى في «خيارات إضافية»: ما عدا الإلزامي بلا قيمة، والصندوق في الجمعيات (أساس صرفها)."""
    keep = {"fund"} if company is not None and company.is_nonprofit else set()
    fields = []
    for name in dt.advanced_fields:
        if name not in form.fields or name in keep or form.fields[name].widget.is_hidden:
            continue
        bound = form[name]
        if form.fields[name].required and bound.value() in (None, ""):
            continue
        fields.append(bound)
    return fields


def _advanced_open(form, fields, company):
    """تُفتح الخيارات الإضافية إن كان فيها خطأ، أو عند تعديل مستند فيه قيمة غير الافتراضية."""
    from decimal import Decimal, InvalidOperation

    def norm(value):
        if value in (None, "", False):
            return ""
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return str(value)
        return "" if number == 0 else str(number.normalize())

    if any(bf.errors for bf in fields):
        return True
    if form.is_bound or form.instance.pk is None:
        return False
    defaults = {"exchange_rate": 1}
    if company:
        defaults.update(currency=company.currency_id, tax_rate=company.default_tax_rate)
    for bf in fields:
        default = defaults.get(bf.name, form.instance._meta.get_field(bf.name).get_default())
        if norm(bf.value()) != norm(default):
            return True
    return False


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


def nonprofit_only_fields(company):
    """حقول الجهات غير الربحية (الصندوق) لا تظهر للمنشأة التجارية."""
    return set() if company is not None and company.is_nonprofit else {"fund"}


def _child_table(dt, obj):
    if not dt.child:
        return [], []
    hidden = nonprofit_only_fields(obj.company if hasattr(obj, "company_id") else None)
    fields = [f for f in (dt.child.display_fields or dt.child.fields) if f not in hidden]
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
    exclude = set(dt.totals) | set(dt.detail_exclude) | {"name", "docstatus"} | nonprofit_only_fields(request.company)
    membership = getattr(request, "membership", None)
    if membership is not None and not membership.may("costs"):
        exclude |= {"buying_rate", "valuation_rate", "stock_value", "price_list_rate"}
    context = {
        "dt": dt, "obj": obj, "doc_currency": getattr(obj, "currency", None), "today": timezone.localdate(),
        "fields": detail_fields(obj, exclude=exclude),
        "totals": [(field_label(dt.model, f), getattr(obj, f)) for f in dt.totals],
        "child_headers": child_headers, "child_rows": child_rows,
        "attachments": list(Attachment.for_object(obj)) if hasattr(obj, "company_id") else None,
    }
    if dt.is_submittable and not obj.is_draft:
        context["gl_entries"], context["stock_entries"] = _linked_entries(obj)
        debit = sum(g.debit for g in context["gl_entries"])
        credit = sum(g.credit for g in context["gl_entries"])
        context["gl_totals"] = {"debit": debit, "credit": credit, "balanced": debit == credit}
    if dt.is_submittable and obj.is_draft:
        from core.approvals import status as approval_status

        context["approvals"] = approval_status(obj)
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
    if dt.is_submittable and not obj.is_draft:
        from core.mail import mail_enabled
        from core.pdf import _party_email, share_path

        context["mail_enabled"] = mail_enabled(request.company)
        context["party_email"] = _party_email(obj)
        context["share_path"] = share_path(request.company, slug, obj)
    if slug in ("customer", "sales-invoice"):
        from messaging.models import MessagingSettings

        cfg = MessagingSettings.all_objects.filter(company=request.company).first()
        context["messaging_active"] = bool(cfg and cfg.active)
    return render(request, "core/detail.html", context)


@login_required
def doc_print(request, slug, pk):
    dt = get_doctype(slug)
    obj = get_object_or_404(_scoped(request, dt), pk=pk)
    membership = getattr(request, "membership", None)
    context = print_page_context(dt, obj, request.company, template=request.GET.get("template"),
                                 can_customize=bool(membership and membership.can_manage))
    return render(request, "core/print.html", context)


def print_page_context(dt, obj, company, template=None, can_customize=False):
    """بيانات صفحة الطباعة؛ تُستخدم للعرض في المتصفح ولتوليد ملف PDF على الخادم."""
    from core.hijri import format_hijri
    from core.models import PrintSettings, PrintTemplate
    from core.printing import print_context

    slug = dt.slug
    ps = PrintSettings.for_company(company)
    if template not in PrintTemplate.values:
        template = ps.template
    child_headers, child_rows = _child_table(dt, obj)
    qr = ""
    if slug == "sales-invoice" and not obj.is_draft:
        from core.zatca import invoice_qr_svg

        qr = invoice_qr_svg(obj)
    p = print_context(slug, obj, company, ps)
    exclude = set(dt.totals) | set(dt.detail_exclude) | {"name", "docstatus", "posting_date", "remarks", "company"}
    posting_date = getattr(obj, "posting_date", None)
    return {
        "qr": qr, "doc_currency": getattr(obj, "currency", None), "p": p, "ps": ps, "template": template,
        "templates": [(v, label.split(" — ")[0]) for v, label in PrintTemplate.choices],
        "can_customize": can_customize,
        "hijri_date": format_hijri(posting_date) if posting_date else "",
        "generic_fields": detail_fields(obj, exclude=exclude) if p["kind"] == "generic" else [],
        "dt": dt, "obj": obj, "child_headers": child_headers, "child_rows": child_rows, "company": company,
        # الجداول العريضة (مثل مسير الرواتب) تُطبع بالعرض وبخط أصغر حتى لا تخرج عن الورقة
        "landscape": len(child_headers) > 7,
        "totals": [(field_label(dt.model, f), getattr(obj, f)) for f in dt.totals if f != "outstanding_amount"],
    }


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
            from core.approvals import approve_or_block

            # الموافقة تُحفظ ولو بقي المستند بانتظار مستوى آخر
            pending = approve_or_block(obj, getattr(request, "membership", None), request.user)
            if pending:
                messages.info(request, pending)
                return redirect("doc_detail", slug=dt.slug, pk=obj.pk)
            obj.submit()
            suffix = " وترحيل قيوده" if getattr(obj, "posts_to_ledger", True) else ""
            messages.success(request, f"تم اعتماد {dt.label} {obj}{suffix}.")
        elif action == "cancel":
            from core.permissions import require_action

            require_action(request, "cancel")
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
    if action == "delete" and dt.read_only:
        messages.error(request, f"لا يمكن حذف {dt.label} يدوياً.")
        return redirect("doc_detail", slug=slug, pk=pk)
    if action == "delete":
        from core.permissions import require_action

        try:
            require_action(request, "delete")
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect("doc_detail", slug=slug, pk=pk)
        if dt.is_submittable and not obj.is_draft:
            messages.error(request, "يمكن حذف المسودات فقط. ألغِ المستند بدلاً من ذلك.")
            return redirect(obj.get_absolute_url())
        try:
            with transaction.atomic():
                object_id = obj.pk
                obj.delete()
                Attachment.objects.filter(doctype=slug, object_id=object_id).delete()
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
    if not sees_costs(request):
        data.pop("buying_rate")
        for unit in data["uoms"].values():
            unit.pop("buying_rate")
    kind = request.GET.get("kind")
    if kind == "buying" and not sees_costs(request):
        kind = None
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
def make_recurring(request, slug, pk):
    """تحويل مستند معتمد إلى مستند متكرر."""
    from core.models import Frequency, add_period
    from core.recurring import make_recurring as create

    dt = get_doctype(slug)
    obj = get_object_or_404(_scoped(request, dt), pk=pk)
    frequency = request.POST.get("frequency") if request.POST.get("frequency") in Frequency.values else Frequency.MONTHLY
    if request.method == "POST":
        from django.utils.dateparse import parse_date

        try:
            if request.POST.get("submit_automatically"):
                from core.permissions import require_submit

                require_submit(request)
            rec = create(obj, frequency, parse_date(request.POST.get("next_date") or ""),
                         parse_date(request.POST.get("end_date") or ""), bool(request.POST.get("submit_automatically")))
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect(obj.get_absolute_url())
        messages.success(request, f"سيُنشأ {dt.label} تلقائياً ({rec.get_frequency_display()}) بدءاً من {rec.next_date:%Y-%m-%d}.")
        return redirect("doc_detail", slug="recurring-document", pk=rec.pk)
    return render(request, "core/make_recurring.html", {
        "dt": dt, "obj": obj, "frequencies": Frequency.choices,
        "next_date": add_period(obj.posting_date, Frequency.MONTHLY)})


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
