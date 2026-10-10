from django.conf import settings

from core.registry import menu


def erp(request):
    context = {"ERP_NAME": settings.ERP_NAME, "ERP_NAME_EN": settings.ERP_NAME_EN}
    if request.user.is_authenticated:
        membership = getattr(request, "membership", None)
        context.update({
            "erp_menu": menu() if membership else [],
            "current_company": getattr(request, "company", None),
            "current_membership": membership,
            "user_memberships": request.user.memberships.filter(is_active=True).select_related("company"),
            "subscription": getattr(getattr(request, "company", None), "subscription", None),
        })
    return context
