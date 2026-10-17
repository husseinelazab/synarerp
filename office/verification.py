from django.shortcuts import get_object_or_404,render
from office.models import Correspondence


def verify(request,token):
    item=get_object_or_404(Correspondence.all_objects.select_related("company"),verification_uuid=token,is_confidential=False)
    response=render(request,"office/verify.html",{"number":item.number,"date":item.date,"company_name":item.company.name})
    response["Cache-Control"]="no-store"
    response["X-Robots-Tag"]="noindex, nofollow"
    return response
