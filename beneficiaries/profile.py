"""A permission-filtered overview; identities are linked explicitly, never by name/phone."""
import datetime
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from core.models import Attachment
from core.registry import REGISTRY
from core.workspace import can_read, scoped
from beneficiaries.models import Beneficiary


@login_required
def unified(request,pk):
    if not request.company.is_nonprofit or not can_read(request,"beneficiary"):
        raise PermissionDenied
    beneficiary=get_object_or_404(scoped(request,Beneficiary),pk=pk)
    sections=[];timeline=[]
    def section(slug,label,filters,date_field):
        dt=REGISTRY.get(slug)
        if not dt or not can_read(request,slug):
            return []
        qs=scoped(request,dt.model).filter(**filters)
        records=list(qs.order_by("-pk")[:30])
        sections.append({"label":label,"rows":records,"count":qs.count(),"url":reverse("doc_list",args=[slug])+f"?beneficiary={beneficiary.pk}"})
        for row in records:
            when=getattr(row,date_field,None) if date_field else None
            if when:
                sort=when.date() if isinstance(when,datetime.datetime) else when
                timeline.append({"date":sort,"label":label,"title":str(row),"url":row.get_absolute_url()})
        return records
    section("case-assessment","دراسات الحالة",{"beneficiary":beneficiary},"posting_date")
    section("aid-disbursement","المساعدات",{"beneficiary":beneficiary},"posting_date")
    section("aid-voucher","قسائم المساعدات",{"beneficiary":beneficiary},"created_at")
    section("sponsorship","الكفالات",{"beneficiary":beneficiary},"start_date")
    section("program-enrollment","البرامج والتدريب",{"beneficiary":beneficiary},"registered_at")
    section("portal-service-request","طلبات الخدمات",{"beneficiary":beneficiary},"created_at")
    section("service-booking","حجوزات الخدمات",{"beneficiary":beneficiary},"created_at")
    section("portal-profile-update","طلبات تحديث البيانات",{"beneficiary":beneficiary},"created_at")
    section("beneficiary-follow-up","المتابعات",{"beneficiary":beneficiary},"created_at")
    students=section("quran-student","الحلقات",{"beneficiary":beneficiary},"joined_on")
    muslims=section("new-muslim","المتابعة الدعوية",{"beneficiary":beneficiary},"conversion_date")
    # Health and trip records use explicit linked person IDs, with independent section permissions.
    patients=section("health-patient","ملفات المرضى",{"beneficiary":beneficiary},None)
    section("dawah-follow-up","المتابعات الدعوية",{"person__beneficiary":beneficiary},"due_date")
    section("health-referral","الإحالات الصحية",{"patient__beneficiary":beneficiary},"requested_on")
    section("quran-learning-plan","خطط الحفظ",{"student__beneficiary":beneficiary},"start_date")
    if students:
        section("quran-progress","الحفظ والمراجعة",{"student__beneficiary":beneficiary},"date")
    if muslims:
        section("umrah-participant","رحلات العمرة",{"participant__beneficiary":beneficiary},"created_at")
    if patients:
        section("health-appointment","المواعيد الصحية",{"patient__beneficiary":beneficiary},"scheduled_at")
    from core.processes import visible_cases
    cases=visible_cases(request).filter(beneficiary=beneficiary)[:30]
    for case in cases:
        timeline.append({"date":case.created_at.date(),"label":"إجراء","title":case.title,"url":case.get_absolute_url()})
    files=Attachment.objects.filter(company=request.company,doctype="beneficiary",object_id=beneficiary.pk).defer("content")
    return render(request,"beneficiaries/unified.html",{"beneficiary":beneficiary,"family":beneficiary.members.prefetch_related("categories"),"sections":sections,"timeline":sorted(timeline,key=lambda x:x["date"],reverse=True)[:60],"files":files,"cases":cases,"can_follow":can_read(request,"beneficiary-follow-up") and request.membership.doctype_access("beneficiary-follow-up")=="write"})
