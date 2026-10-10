from django.conf import settings

from core.registry import menu


def erp(request):
    return {"ERP_NAME": settings.ERP_NAME, "erp_menu": menu() if request.user.is_authenticated else []}
