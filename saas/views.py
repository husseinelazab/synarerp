import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.middleware import SESSION_KEY
from core.models import Company, Membership, Role, OrganizationType
from core.provisioning import provision_company
from core.utils import client_ip
from saas.backups import backup_status
from saas.forms import (CompanySettingsForm, MemberCreateForm, MemberRoleForm, PlanForm, RecordPaymentForm,
                        SignupForm)
from saas.models import Plan, Subscription, SubscriptionPayment, SubscriptionStatus
from saas.services import active_user_count, check_user_limit, ensure_default_plans, record_payment

User = get_user_model()


def _error(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


# ---------------- الصفحات العامة ----------------

def landing(request):
    plans = Plan.objects.filter(is_public=True, is_active=True)
    return render(request, "saas/landing.html", {
        "plans": plans,
        "plan_groups": [(label, plans.filter(organization_type=kind)) for kind, label in OrganizationType.choices],
    })


# حد التسجيل من عنوان IP واحد: كل تسجيل يبني شركة كاملة (شجرة حسابات وإعدادات)، فلا يُترك مفتوحاً للبرامج الآلية
SIGNUP_LIMIT = int(os.environ.get("SYNAR_SIGNUP_LIMIT", "5"))
SIGNUP_WINDOW_SECONDS = 24 * 3600
# حقل مخفي عن المستخدم (مصيدة): البرامج الآلية تملأ كل الحقول فيُكشف بها
SIGNUP_HONEYPOT = "website_url"


def _signup_key(request):
    return f"signup:ip:{client_ip(request)}"


def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    initial = {}
    if request.GET.get("plan"):
        initial["plan"] = Plan.objects.filter(code=request.GET["plan"], is_active=True, is_public=True).first()
        if initial["plan"]:
            initial["organization_type"] = initial["plan"].organization_type
    form = SignupForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if request.POST.get(SIGNUP_HONEYPOT):
            return render(request, "saas/signup.html", {"form": SignupForm(initial=initial)}, status=400)
        if (cache.get(_signup_key(request)) or 0) >= SIGNUP_LIMIT:
            form.add_error(None, "تجاوزت عدد الحسابات المسموح بإنشائها من هذا الجهاز اليوم. حاول غداً أو تواصل مع الدعم.")
            return render(request, "saas/signup.html", {"form": form}, status=429)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        first, _, last = data["full_name"].partition(" ")
        with transaction.atomic():
            from core.scoped_auth import create_company_user
            company = provision_company(data["company_name"], tax_id=data["tax_id"], phone=data["phone"],
                                        plan=data["plan"], organization_type=data["organization_type"])
            user = create_company_user(company, data["username"], data["email"], data["password"], first_name=first, last_name=last)
            Membership.objects.create(user=user, company=company, role=Role.OWNER, login_name=data["username"])
        key = _signup_key(request)
        if cache.add(key, 1, SIGNUP_WINDOW_SECONDS) is False:
            cache.incr(key)
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        request.session[SESSION_KEY] = company.pk
        messages.success(request, f"مرحباً بك في {settings.ERP_NAME}! تم تأسيس «{company}» وبدأت فترتك التجريبية "
                                  f"لمدة {company.subscription.plan.trial_days} يوماً.")
        return redirect("dashboard")
    return render(request, "saas/signup.html", {"form": form})


# ---------------- داخل الشركة ----------------

@login_required
@require_POST
def switch_company(request, pk):
    member = Membership.objects.filter(user=request.user, company_id=pk, is_active=True).exists()
    if request.user.is_superuser and not member and not Company.objects.filter(pk=pk, allow_support_access=True).exists():
        messages.error(request, "أوقفت هذه الشركة دخول فريق الدعم الفني من إعداداتها.")
        return redirect("platform_dashboard")
    allowed = request.user.is_superuser or member
    if allowed:
        request.session[SESSION_KEY] = pk
    return redirect("dashboard")


@login_required
def billing(request):
    subscription = getattr(request.company, "subscription", None)
    return render(request, "saas/billing.html", {
        "subscription": subscription,
        "payments": subscription.payments.all() if subscription else [],
        "plans": Plan.objects.filter(is_public=True, is_active=True, organization_type=request.company.organization_type),
        "users_count": active_user_count(request.company),
        "instructions": settings.SYNAR_BILLING_INSTRUCTIONS,
        "support": settings.SYNAR_SUPPORT_CONTACT,
    })


@login_required
def company_settings(request):
    form = CompanySettingsForm(request.POST or None, instance=request.company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ إعدادات الشركة.")
        return redirect("company_settings")
    return render(request, "settings/company.html", {"form": form})


@login_required
def users_list(request):
    # حسابات بوابة الموظفين تُدار من صفحة الموظف
    members = request.company.memberships.exclude(role="employee").select_related("user", "role_policy", "organization_unit").order_by("-is_active", "id")
    subscription = getattr(request.company, "subscription", None)
    return render(request, "settings/users.html", {
        "members": members, "max_users": subscription.plan.max_users if subscription else None,
        "active_count": active_user_count(request.company),
    })


@login_required
def user_create(request):
    form = MemberCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            check_user_limit(request.company)
        except ValidationError as exc:
            form.add_error(None, _error(exc))
        else:
            first, _, last = data["full_name"].partition(" ")
            with transaction.atomic():
                from core.scoped_auth import create_company_user
                user = create_company_user(request.company, data["username"], data["email"], data["password"], first_name=first, last_name=last)
                Membership.objects.create(user=user, company=request.company, role=data["role"], login_name=data["username"])
            messages.success(request, f"تمت إضافة المستخدم «{data['username']}».")
            return redirect("users_list")
    return render(request, "settings/user_form.html", {"form": form, "title": "إضافة مستخدم"})


@login_required
def user_edit(request, pk):
    member = get_object_or_404(Membership, pk=pk, company=request.company)
    if member.role == Role.OWNER:
        messages.error(request, "لا يمكن تعديل دور مالك الحساب.")
        return redirect("users_list")
    if member.role == Role.EMPLOYEE:
        messages.info(request, "حسابات بوابة الموظفين تُدار من صفحة الموظف.")
        return redirect("users_list")
    form = MemberRoleForm(request.POST or None, initial={
        "role": member.role, "modules": member.modules, "can_submit": member.can_submit,
        "role_policy": member.role_policy_id, "organization_unit": member.organization_unit_id,
        "cost_centers": list(member.cost_centers.all()), "can_cancel": member.can_cancel,
        "can_delete": member.can_delete, "can_export": member.can_export, "hide_costs": member.hide_costs,
        "denied_doctypes": member.denied_doctypes, "read_only_doctypes": member.read_only_doctypes})
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("deactivate", "activate"):
            if action == "activate":
                try:
                    check_user_limit(request.company)
                except ValidationError as exc:
                    messages.error(request, _error(exc))
                    return redirect("users_list")
            member.is_active = action == "activate"
            member.save(update_fields=["is_active"])
            messages.success(request, "تم تحديث حالة المستخدم.")
            return redirect("users_list")
        if form.is_valid():
            data = form.cleaned_data
            fields = ["role", "modules", "can_submit", "can_cancel", "can_delete", "can_export", "hide_costs",
                      "denied_doctypes", "read_only_doctypes"]
            for name in fields:
                setattr(member, name, data[name])
            if request.company.is_nonprofit:
                for name in ("role_policy", "organization_unit"):
                    setattr(member, name, data.get(name))
                    fields.append(name)
            member.save(update_fields=fields)
            member.cost_centers.set(data["cost_centers"])
            messages.success(request, "تم تحديث دور المستخدم وصلاحياته.")
            return redirect("users_list")
    return render(request, "settings/user_form.html", {"form": form, "member": member, "title": f"تعديل {member.user}"})


# ---------------- لوحة مدير المنصة ----------------

def _superuser(user):
    return user.is_active and user.is_superuser


@user_passes_test(_superuser)
def platform_dashboard(request):
    today = timezone.localdate()
    subs = Subscription.objects.select_related("company", "plan")
    query = request.GET.get("q", "").strip()
    companies = Company.objects.select_related("subscription__plan").annotate(
        users=Count("memberships", filter=Q(memberships__is_active=True))).order_by("-id")
    if query:
        companies = companies.filter(Q(name__icontains=query) | Q(tax_id__icontains=query) | Q(phone__icontains=query))
    valid = subs.filter(status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE], period_end__gte=today)
    paying = valid.filter(status=SubscriptionStatus.ACTIVE)
    stats = [
        ("إجمالي الشركات", Company.objects.count()),
        ("اشتراكات مدفوعة نشطة", paying.count()),
        ("في الفترة التجريبية", valid.filter(status=SubscriptionStatus.TRIAL).count()),
        ("منتهية أو موقوفة", subs.count() - valid.count()),
        ("الإيراد الشهري المتكرر (تقديري)", paying.aggregate(v=Sum("plan__price_monthly"))["v"] or 0),
        ("تحصيلات هذا الشهر", SubscriptionPayment.objects.filter(
            created_at__date__gte=today.replace(day=1)).aggregate(v=Sum("amount"))["v"] or 0),
    ]
    return render(request, "platform/dashboard.html", {
        "stats": stats, "companies": companies[:300], "query": query,
        "expiring": valid.filter(period_end__lte=today + timezone.timedelta(days=7)).order_by("period_end"),
        "backup": backup_status(), "isolation": _isolation_status(),
    })


def _isolation_status():
    from core import rls

    try:
        return rls.status()
    except Exception:  # عرض فقط: لا يعطل لوحة المنصة
        return {"available": False}


@user_passes_test(_superuser)
def platform_company(request, pk):
    company = get_object_or_404(Company.objects.select_related("subscription__plan"), pk=pk)
    subscription = getattr(company, "subscription", None)
    form = RecordPaymentForm(request.POST or None, company=company, initial={"plan": subscription.plan if subscription else None})
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "payment" and form.is_valid():
            data = form.cleaned_data
            record_payment(subscription, data["plan"], data["months"], data["amount"], data["method"],
                           data["reference"], request.user)
            messages.success(request, "تم تسجيل الدفعة وتمديد الاشتراك.")
            return redirect("platform_company", pk=pk)
        if action in ("suspend", "reactivate") and subscription:
            subscription.status = SubscriptionStatus.SUSPENDED if action == "suspend" else SubscriptionStatus.ACTIVE
            subscription.save(update_fields=["status"])
            messages.success(request, "تم تحديث حالة الاشتراك.")
            return redirect("platform_company", pk=pk)
        if action == "enter":
            if not company.allow_support_access and not company.memberships.filter(user=request.user).exists():
                messages.error(request, "أوقفت هذه الشركة دخول فريق الدعم الفني من إعداداتها.")
                return redirect("platform_company", pk=company.pk)
            request.session[SESSION_KEY] = company.pk
            return redirect("dashboard")
    return render(request, "platform/company.html", {
        "company": company, "subscription": subscription, "form": form,
        "members": company.memberships.select_related("user"),
        "payments": subscription.payments.select_related("plan") if subscription else [],
    })


@user_passes_test(_superuser)
def platform_plans(request):
    from core.permissions import PLAN_MODULES

    kind = request.GET.get("organization_type", "business")
    if kind not in ("business", "nonprofit"):
        kind = "business"
    archived = request.GET.get("archived") == "1"
    plans = list(Plan.objects.filter(organization_type=kind, is_active=not archived).annotate(companies=Count("subscription")))
    matrix = [(label, [p.allows_module(key) for p in plans]) for key, label in PLAN_MODULES]
    matrix.append(("قراءة الفواتير بالذكاء الاصطناعي", [p.includes_ai for p in plans]))
    return render(request, "platform/plans.html", {"plans": plans, "matrix": matrix, "organization_type": kind})


@user_passes_test(_superuser)
def platform_plan_edit(request, pk=None):
    plan = get_object_or_404(Plan, pk=pk) if pk else None
    form = PlanForm(request.POST or None, instance=plan)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ الباقة، وتسري على كل الشركات المشتركة فيها فوراً.")
        return redirect("platform_plans")
    return render(request, "settings/user_form.html", {
        "form": form, "title": f"الباقة: {plan}" if plan else "باقة جديدة", "back_url": "platform_plans"})


@user_passes_test(_superuser)
@require_POST
def platform_plan_archive(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    plan.is_active = False
    plan.is_public = False
    plan.save(update_fields=["is_active", "is_public"])
    messages.success(request, "حُذفت الباقة من العرض والاشتراكات الجديدة؛ حُفظ سجل المشتركين والمدفوعات السابق.")
    return redirect("platform_plans")
