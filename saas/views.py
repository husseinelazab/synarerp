from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.middleware import SESSION_KEY
from core.models import Company, Membership, Role
from core.provisioning import provision_company
from saas.forms import (CompanySettingsForm, MemberCreateForm, MemberRoleForm, RecordPaymentForm,
                        SignupForm)
from saas.models import Plan, Subscription, SubscriptionPayment, SubscriptionStatus
from saas.services import active_user_count, check_user_limit, ensure_default_plans, record_payment

User = get_user_model()


def _error(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


# ---------------- الصفحات العامة ----------------

def landing(request):
    ensure_default_plans()
    return render(request, "saas/landing.html", {"plans": Plan.objects.filter(is_public=True)})


def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    ensure_default_plans()
    initial = {}
    if request.GET.get("plan"):
        initial["plan"] = Plan.objects.filter(code=request.GET["plan"]).first()
    form = SignupForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        first, _, last = data["full_name"].partition(" ")
        with transaction.atomic():
            user = User.objects.create_user(data["username"], data["email"], data["password"],
                                            first_name=first, last_name=last)
            company = provision_company(data["company_name"], owner=user, tax_id=data["tax_id"],
                                        phone=data["phone"], plan=data["plan"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session[SESSION_KEY] = company.pk
        messages.success(request, f"مرحباً بك في {settings.ERP_NAME}! تم تأسيس «{company}» وبدأت فترتك التجريبية "
                                  f"لمدة {company.subscription.plan.trial_days} يوماً.")
        return redirect("dashboard")
    return render(request, "saas/signup.html", {"form": form})


# ---------------- داخل الشركة ----------------

@login_required
@require_POST
def switch_company(request, pk):
    allowed = request.user.is_superuser or Membership.objects.filter(user=request.user, company_id=pk, is_active=True).exists()
    if allowed:
        request.session[SESSION_KEY] = pk
    return redirect("dashboard")


@login_required
def billing(request):
    subscription = getattr(request.company, "subscription", None)
    return render(request, "saas/billing.html", {
        "subscription": subscription,
        "payments": subscription.payments.all() if subscription else [],
        "plans": Plan.objects.filter(is_public=True),
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
    members = request.company.memberships.exclude(role="employee").select_related("user").order_by("-is_active", "id")
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
                user = User.objects.create_user(data["username"], data["email"], data["password"],
                                                first_name=first, last_name=last)
                Membership.objects.create(user=user, company=request.company, role=data["role"])
            messages.success(request, f"تمت إضافة المستخدم «{user.username}».")
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
        "cost_centers": list(member.cost_centers.all())})
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
            member.role, member.modules, member.can_submit = data["role"], data["modules"], data["can_submit"]
            member.save(update_fields=["role", "modules", "can_submit"])
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
    })


@user_passes_test(_superuser)
def platform_company(request, pk):
    company = get_object_or_404(Company.objects.select_related("subscription__plan"), pk=pk)
    subscription = getattr(company, "subscription", None)
    form = RecordPaymentForm(request.POST or None, initial={"plan": subscription.plan if subscription else None})
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
            request.session[SESSION_KEY] = company.pk
            return redirect("dashboard")
    return render(request, "platform/company.html", {
        "company": company, "subscription": subscription, "form": form,
        "members": company.memberships.select_related("user"),
        "payments": subscription.payments.select_related("plan") if subscription else [],
    })
