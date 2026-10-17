from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from core.models import Notification
from core.workspace import can_read
from portal.models import ServiceSlot, ServiceBooking
from portal.site_views import portal_page, _render
from website.site import site_url


@portal_page
def appointments(request):
    company=request.site.company
    if request.method=="POST":
        with transaction.atomic():
            if request.POST.get("action")=="cancel":
                booking=get_object_or_404(ServiceBooking.objects.select_for_update(),pk=request.POST.get("booking"),company=company,beneficiary=request.beneficiary)
                if booking.status in ("pending","confirmed") and booking.slot.starts_at>timezone.now():
                    booking.status="cancelled";booking.save(update_fields=["status","updated_at"])
                    messages.success(request,"أُلغي الحجز وأُتيح المقعد من جديد.")
            else:
                slot=get_object_or_404(ServiceSlot.objects.select_for_update(),pk=request.POST.get("slot"),company=company,published=True,starts_at__gt=timezone.now())
                duplicate=ServiceBooking.objects.filter(company=company,slot=slot,beneficiary=request.beneficiary,status__in=["pending","confirmed"]).exists()
                used=ServiceBooking.objects.filter(company=company,slot=slot,status__in=["pending","confirmed"]).count()
                if duplicate:
                    messages.info(request,"لديك حجز لهذا الموعد بالفعل.")
                elif used>=slot.capacity:
                    messages.error(request,"اكتملت المقاعد؛ اختر موعدًا آخر.")
                else:
                    booking=ServiceBooking.objects.create(company=company,slot=slot,beneficiary=request.beneficiary,note=request.POST.get("note","").strip()[:500])
                    Notification.objects.create(company=company,user_id=slot.officer_id,title=f"حجز خدمة جديد: {slot.title}"[:255],url=booking.get_absolute_url())
                    messages.success(request,"حُجز المقعد بانتظار تأكيد الجمعية. تابع حالة الحجز هنا.")
        return redirect(site_url(request,"portal_appointments"))
    slots=ServiceSlot.objects.filter(company=company,published=True,starts_at__gt=timezone.now()).annotate(used=Count("bookings",filter=Q(bookings__status__in=["pending","confirmed"])))
    bookings=ServiceBooking.objects.filter(company=company,beneficiary=request.beneficiary).select_related("slot")[:50]
    return _render(request,"portal/appointments.html","portal_appointments",slots=slots,bookings=bookings,now=timezone.now())


@login_required
@require_POST
def review(request,pk):
    if not can_read(request,"service-booking") or request.membership.doctype_access("service-booking")!="write" or not request.membership.may_submit:
        raise PermissionDenied
    with transaction.atomic():
        booking=get_object_or_404(ServiceBooking.objects.select_for_update(),pk=pk,company=request.company)
        if not request.membership.can_manage and booking.slot.officer_id!=request.user.pk:
            raise PermissionDenied
        decision=request.POST.get("decision")
        transitions={"pending":{"confirmed","rejected","cancelled"},"confirmed":{"completed","cancelled"}}
        reply=request.POST.get("reply","").strip()[:1000]
        if decision not in transitions.get(booking.status,set()):
            messages.error(request,"الحجز تغير بالفعل أو انتهى؛ راجع حالته الحالية.")
        elif decision in ("rejected","cancelled") and not reply:
            messages.error(request,"اكتب سببًا ظاهرًا للمستفيد.")
        elif decision=="completed" and booking.slot.starts_at>timezone.now():
            messages.error(request,"لا يُسجل إنجاز اللقاء قبل موعده.")
        else:
            booking.status=decision;booking.reply=reply;booking.reviewed_by=request.user
            booking.save(update_fields=["status","reply","reviewed_by","updated_at"])
    return redirect(booking.get_absolute_url())
