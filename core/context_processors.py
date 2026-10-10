from django.conf import settings

from core.navigation import ICONS, build_menu


def erp(request):
    context = {"ERP_NAME": settings.ERP_NAME, "ERP_NAME_EN": settings.ERP_NAME_EN}
    if not request.user.is_authenticated and request.path.startswith("/login/"):
        from core.password_reset import reset_available

        context["PASSWORD_RESET_ENABLED"] = reset_available()
    if request.user.is_authenticated:
        membership = getattr(request, "membership", None)
        context.update({
            "nav_modules": build_menu(request),
            "home_icon": ICONS["home"],
            "current_company": getattr(request, "company", None),
            "current_membership": membership,
            "user_memberships": request.user.memberships.filter(is_active=True).select_related("company"),
            "subscription": getattr(getattr(request, "company", None), "subscription", None),
        })
        company = getattr(request, "company", None)
        if company is not None:
            from core.models import Notification

            context["unread_notifications"] = Notification.all_objects.filter(
                company=company, user=request.user, is_read=False).count()
    return context
