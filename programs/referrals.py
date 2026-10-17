from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404,redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from core.workspace import can_read
from programs.models import HealthReferral


@login_required
@require_POST
def review(request,pk):
    if not can_read(request,"health-referral") or request.membership.doctype_access("health-referral")!="write" or not request.membership.may_submit:
        raise PermissionDenied
    with transaction.atomic():
        from core.models import Company
        Company.objects.select_for_update().get(pk=request.company.pk)
        item=get_object_or_404(HealthReferral.objects.select_for_update(),pk=pk,company=request.company)
        decision=request.POST.get("decision");note=request.POST.get("note","").strip()[:1000]
        transitions={"requested":{"approved","rejected","cancelled"},"approved":{"completed","cancelled"}}
        if decision not in transitions.get(item.status,set()):
            messages.error(request,"تغيرت حالة الإحالة أو انتهت بالفعل.")
        elif decision in ("approved","rejected") and not request.membership.can_manage:
            raise PermissionDenied
        elif decision in ("rejected","cancelled") and not note:
            messages.error(request,"اكتب سبب القرار الإداري.")
        elif decision=="completed" and item.appointment_at and item.appointment_at>timezone.now():
            messages.error(request,"لا تسجل اكتمال الخدمة قبل الموعد المقترح.")
        else:
            item.status=decision;item.reviewed_by=request.user;item.reviewed_at=timezone.now();item.decision_note=note
            item.save(update_fields=["status","reviewed_by","reviewed_at","decision_note"])
    return redirect(item.get_absolute_url())
