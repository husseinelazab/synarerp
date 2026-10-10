from django.conf import settings

from core.registry import menu


def erp(request):
    return {"ERP_NAME": settings.ERP_NAME, "ERP_NAME_EN": settings.ERP_NAME_EN, "erp_menu": menu() if request.user.is_authenticated else []}
