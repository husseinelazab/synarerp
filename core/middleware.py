"""تحديد الشركة الحالية لكل طلب وتطبيق الصلاحيات وصلاحية الاشتراك."""
from django.contrib import messages
from django.shortcuts import redirect, render

from core.models import Company, Membership, Role
from core.tenancy import _current_ip, _current_user, reset_current_company, set_current_company, set_current_user
from core.utils import client_ip

SESSION_KEY = "company_id"

# مسارات لا تحتاج شركة حالية
PUBLIC_PREFIXES = ("/static/", "/admin/", "/login/", "/logout/", "/signup/", "/favicon.ico", "/password-reset/", "/healthz/", "/api/v1/", "/iclock/")
# مسارات متاحة حتى مع انتهاء الاشتراك
BILLING_SAFE_PREFIXES = ("/billing/", "/switch-company/", "/platform/")
# ما يُسمح للكاشير بالوصول إليه
CASHIER_PREFIXES = ("/pos/", "/api/item/", "/switch-company/", "/account/")
# بوابة الخدمة الذاتية للموظف
EMPLOYEE_PREFIXES = ("/me/", "/switch-company/", "/account/")
# مسموحة قبل تفعيل التحقق الثنائي الإلزامي
TWO_FACTOR_SAFE_PREFIXES = ("/account/", "/switch-company/", "/billing/")
# صفحات الإدارة لمالك الحساب والمدير فقط
MANAGER_PREFIXES = ("/settings/", "/billing/", "/einvoice/settings/", "/hr/settings/", "/app/approval-rule/")


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.company = None
        request.membership = None
        path = request.path
        if not request.user.is_authenticated or path.startswith(PUBLIC_PREFIXES):
            return self.get_response(request)

        membership = self._resolve_membership(request)
        if membership is None:
            if path.startswith("/platform/"):
                return self.get_response(request)
            if request.user.is_superuser:
                return redirect("platform_dashboard")
            return render(request, "core/no_company.html", status=403)

        request.membership, request.company = membership, membership.company
        token = set_current_company(membership.company)
        user_token = set_current_user(request.user)
        ip_token = _current_ip.set(client_ip(request))
        try:
            if membership.pk is None:  # مدير المنصة في وضع الدعم الفني
                from core.audit import log_support_access

                log_support_access(request, membership.company)
            blocked = self._check_access(request, path)
            return blocked or self.get_response(request)
        finally:
            reset_current_company(token)
            _current_user.reset(user_token)
            _current_ip.reset(ip_token)

    def _resolve_membership(self, request):
        user = request.user
        company_id = request.session.get(SESSION_KEY)
        memberships = Membership.objects.filter(user=user, is_active=True).select_related(
            "company", "company__subscription__plan")
        membership = memberships.filter(company_id=company_id).first() if company_id else None
        if membership is None and user.is_superuser and company_id:
            # مدير المنصة يدخل أي شركة لأغراض الدعم الفني بصلاحية المالك (دون حفظ عضوية)
            company = Company.objects.filter(pk=company_id).first()
            if company and company.allow_support_access:
                return Membership(user=user, company=company, role=Role.OWNER)
        if membership is None:
            membership = memberships.order_by("id").first()
            if membership:
                request.session[SESSION_KEY] = membership.company_id
        return membership

    def _check_access(self, request, path):
        membership = request.membership
        if membership.role == Role.CASHIER and not path.startswith(CASHIER_PREFIXES):
            return redirect("pos_terminal")
        if membership.role == Role.EMPLOYEE and not path.startswith(EMPLOYEE_PREFIXES):
            return redirect("portal_home")
        if path.startswith(MANAGER_PREFIXES) and not membership.can_manage:
            messages.error(request, "هذه الصفحة متاحة لمالك الحساب والمدير فقط.")
            return redirect("dashboard")
        if membership.company.require_two_factor and not path.startswith(TWO_FACTOR_SAFE_PREFIXES):
            from core.models import has_two_factor

            if not has_two_factor(request.user):
                messages.warning(request, "تشترط شركتك تفعيل التحقق الثنائي قبل استخدام النظام.")
                return redirect("two_factor_setup")
        from core.permissions import path_allowed

        if not path_allowed(membership, path):
            messages.error(request, "ليست لديك صلاحية على هذه الوحدة. تواصل مع مدير الشركة.")
            return redirect("dashboard")

        subscription = getattr(membership.company, "subscription", None)
        if subscription is None or not subscription.is_valid:
            if not path.startswith(BILLING_SAFE_PREFIXES):
                if membership.can_manage:
                    messages.warning(request, "انتهى اشتراك الشركة أو تم إيقافه. يرجى التجديد لمتابعة استخدام النظام.")
                    return redirect("billing")
                return render(request, "saas/expired.html", status=402)
        else:
            from core.permissions import plan_allows_path

            if not plan_allows_path(subscription.plan, path):
                if membership.role == Role.CASHIER:  # الكاشير لا يصل لغير نقطة البيع، فلا نحوّله في حلقة
                    return render(request, "saas/module_closed.html", status=402)
                messages.warning(request, "هذه الوحدة غير متاحة في باقتك الحالية. يمكنك الترقية من صفحة الاشتراك.")
                return redirect("billing" if membership.can_manage else "dashboard")
        return None
