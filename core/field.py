"""Assigned visits, dawah follow-ups, trip checklists and Quran attendance with idempotent sync."""
import datetime
import hashlib
import json
import uuid
from decimal import Decimal,InvalidOperation
from pathlib import Path
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.http import HttpResponse,JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie
from core.models import Company
from core.workspace import can_read,scoped
from beneficiaries.models import BeneficiaryFollowUp,FieldSubmission
from programs.models import DawahFollowUp,UmrahChecklist,QuranStudent,QuranProgress

MODELS={"beneficiary-follow-up":BeneficiaryFollowUp,"dawah-follow-up":DawahFollowUp,"umrah-checklist":UmrahChecklist}


def supported(request):
    kinds=[k for k in MODELS if can_read(request,k)]
    if can_read(request,"quran-student") and can_read(request,"quran-progress"):kinds.append("quran-progress")
    return kinds


def allowed(request,kind=None,write=False):
    kinds=supported(request)
    if not kinds or kind and kind not in kinds or write and request.membership.doctype_access(kind)!="write":raise PermissionDenied


@login_required
@ensure_csrf_cookie
def app(request):
    allowed(request)
    return HttpResponse((Path(settings.BASE_DIR)/"static/field/index.html").read_text(),content_type="text/html")


@login_required
def worker(request):
    allowed(request)
    response=HttpResponse((Path(settings.BASE_DIR)/"static/field/sw.js").read_text(),content_type="application/javascript")
    response["Cache-Control"]="no-cache";response["Service-Worker-Allowed"]="/work/field/"
    return response


@login_required
def data(request):
    allowed(request);tasks=[];today=timezone.localdate()
    for kind,model in MODELS.items():
        if kind not in supported(request):continue
        rows=scoped(request,model).filter(assigned_to=request.user,completed=False)[:100]
        for row in rows:
            file=row.beneficiary.file_number if kind=="beneficiary-follow-up" else "متابعة دعوية" if kind=="dawah-follow-up" else "تجهيز رحلة عمرة"
            tasks.append({"kind":kind,"id":row.pk,"title":row.subject if kind=="beneficiary-follow-up" else row.title,"file":file,"due":str(row.due_date or ""),"version":row.version})
    if "quran-progress" in supported(request):
        students=list(scoped(request,QuranStudent).filter(circle__supervisor=request.user,circle__disabled=False,disabled=False)[:100])
        progress={p.student_id:p for p in scoped(request,QuranProgress).filter(student_id__in=[s.pk for s in students],date=today)}
        for student in students:
            current=progress.get(student.pk)
            tasks.append({"kind":"quran-progress","id":student.pk,"title":student.full_name,"file":"حضور وحفظ الحلقة","due":str(today),"day":str(today),"version":current.version if current else 0,"attended":current.attended if current else True,"pages":str(current.pages_memorized) if current else "0","score":current.score if current else None})
    response=JsonResponse({"profile":f"{request.company.pk}:{request.user.pk}","company":request.company.name,"tasks":tasks})
    response["Cache-Control"]="no-store";return response


@login_required
@require_POST
def sync(request):
    try:
        if len(request.body)>16384:raise ValueError
        payload=json.loads(request.body)
        kind=payload.get("kind","beneficiary-follow-up")
        if kind not in (*MODELS,"quran-progress"):raise ValueError
        allowed(request,kind,True)
        token=uuid.UUID(payload["token"]);pk=int(payload["id"]);version=int(payload["version"])
        note=payload["note"].strip();completed=payload.get("completed",False)
        if not note or len(note)>2000 or type(completed) is not bool:raise ValueError
        normalized={"kind":kind,"id":pk,"version":version,"note":note,"completed":completed}
        if kind=="quran-progress":
            day=datetime.date.fromisoformat(payload["day"]);attended=payload["attended"];pages=Decimal(str(payload.get("pages","0")));score=payload.get("score")
            if score is not None:score=int(score)
            if day>timezone.localdate() or type(attended) is not bool or not pages.is_finite() or pages<0 or pages>99999 or pages.as_tuple().exponent < -2 or not attended and pages or score is not None and not 0<=score<=100:raise ValueError
            normalized.update(day=str(day),attended=attended,pages=str(pages),score=score)
    except (ValueError,TypeError,KeyError,AttributeError,InvalidOperation):return JsonResponse({"error":"بيانات المزامنة غير صالحة."},status=400)
    fingerprint=hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with transaction.atomic():
        # All specialty writes lock the company before individual rows.
        if kind!="beneficiary-follow-up":Company.objects.select_for_update().get(pk=request.company.pk)
        student=None
        if kind=="quran-progress":
            student=scoped(request,QuranStudent).filter(pk=pk,circle__supervisor=request.user,circle__disabled=False,disabled=False).first()
            row=scoped(request,QuranProgress).select_for_update().filter(student_id=pk,date=day).first() if student else None
            authorized=student is not None
        else:
            row=scoped(request,MODELS[kind]).select_for_update().filter(pk=pk,assigned_to=request.user).first();authorized=row is not None
        if not authorized:return JsonResponse({"error":"السجل لم يعد مسندًا إليك أو غير متاح."},status=403)
        previous=FieldSubmission.objects.filter(company=request.company,token=token).first()
        if previous:
            if previous.actor_id!=request.user.pk or previous.fingerprint!=fingerprint:return JsonResponse({"error":"معرّف المزامنة مستخدم لبيانات مختلفة."},status=409)
            return JsonResponse({"ok":True,"version":previous.result_version,"duplicate":True})
        actual_version=row.version if row else 0
        if actual_version!=version or kind!="quran-progress" and row.completed:return JsonResponse({"error":"تغير السجل منذ تنزيله. راجع النسخة الحالية؛ الملاحظة لم تُحذف من الجهاز."},status=409)
        stamp=timezone.localtime().strftime("%Y/%m/%d %H:%M")
        text=f"متابعة ميدانية {stamp} — {request.user.get_full_name() or request.user.username}\n{note}"
        if kind=="quran-progress":
            if row is None:row=QuranProgress(company=request.company,student=student,date=day)
            row.attended=attended;row.pages_memorized=pages;row.score=score;row.notes=(row.notes+"\n\n"+text).strip();row.save()
        else:
            field="outcome" if kind=="dawah-follow-up" else "notes"
            setattr(row,field,(getattr(row,field)+"\n\n"+text).strip());row.completed=completed;row.save(update_fields=[field,"completed"])
        FieldSubmission.objects.create(company=request.company,follow_up=row if kind=="beneficiary-follow-up" else None,doctype=kind,object_id=pk,actor=request.user,token=token,fingerprint=fingerprint,result_version=row.version)
    return JsonResponse({"ok":True,"version":row.version})
