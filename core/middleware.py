"""تحديد الشركة الحالية لكل طلب وتطبيق الصلاحيات وصلاحية الاشتراك."""
from django.contrib import messages
from django.shortcuts import redirect, render

from core.models import Company, Membership, Role
from core.tenancy import reset_current_company, set_current_company

SESSION_KEY = "company_id"

# مسارات لا تحتاج شركة حالية
PUBLIC_PREFIXES = ("/static/", "/admin/", "/login/", "/logout/", "/signup/", "/favicon.ico", "/password-reset/", "/healthz/")
# مسارات متاحة حتى مع انتهاء الاشتراك
BILLING_SAFE_PREFIXES = ("/billing/", "/switch-company/", "/platform/")
# ما يُسمح للكاشير بالوصول إليه
CASHIER_PREFIXES = ("/pos/", "/api/item/", "/switch-company/", "/account/")
# بوابة الخدمة الذاتية للموظف
EMPLOYEE_PREFIXES = ("/me/", "/switch-company/", "/account/")
# صفحات الإدارة لمالك الحساب والمدير فقط
MANAGER_PREFIXES = ("/settings/", "/billing/", "/einvoice/settings/", "/hr/settings/")


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
        try:
            blocked = self._check_access(request, path)
            return blocked or self.get_response(request)
        finally:
            reset_current_company(token)

    def _resolve_membership(self, request):
        user = request.user
        company_id = request.session.get(SESSION_KEY)
        memberships = Membership.objects.filter(user=user, is_active=True).select_related(
            "company", "company__subscription__plan")
        membership = memberships.filter(company_id=company_id).first() if company_id else None
        if membership is None and user.is_superuser and company_id:
            # مدير المنصة يدخل أي شركة لأغراض الدعم الفني بصلاحية المالك (دون حفظ عضوية)
            company = Company.objects.filter(pk=company_id).first()
            if company:
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

        subscription = getattr(membership.company, "subscription", None)
        if subscription is None or not subscription.is_valid:
            if not path.startswith(BILLING_SAFE_PREFIXES):
                if membership.can_manage:
                    messages.warning(request, "انتهى اشتراك الشركة أو تم إيقافه. يرجى التجديد لمتابعة استخدام النظام.")
                    return redirect("billing")
                return render(request, "saas/expired.html", status=402)
        elif path.startswith("/pos/") and not subscription.plan.includes_pos:
            messages.warning(request, "نقاط البيع غير متاحة في باقتك الحالية. يمكنك الترقية من صفحة الاشتراك.")
            return redirect("billing" if membership.can_manage else "dashboard")
        return None
