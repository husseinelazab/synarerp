"""Reviewed outcome evidence and explicit, permission-gated accounting costs."""
import csv
import datetime
from decimal import Decimal
from io import BytesIO

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Count, OuterRef, Subquery, Sum
from django.http import HttpResponse, FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.forms import style_form
from core.models import Membership
from core.permissions import allowed_centers
from core.utils import flt, csv_safe
from core.workspace import can_module, can_read, scoped
from programs.models import ImpactGoal, ImpactMeasurement, ProgramCostLink, TrainingProgram, ProgramStatus, ProgramEnrollment, ADMITTED_STATUSES


def modules(request):
    if not request.company.is_nonprofit:
        return []
    choices=ImpactGoal._meta.get_field("module").choices
    return [(key,label) for key,label in choices if can_module(request,key) and (key!="programs" or can_read(request,"training-program"))]


def goals(request):
    return ImpactGoal.objects.filter(company=request.company,module__in=[k for k,v in modules(request)]).select_related("owner","program")


def require_access(request):
    if not modules(request):
        raise PermissionDenied


def cost_access(request):
    return request.membership.may("costs") and allowed_centers(request) is None and can_module(request,"accounts") and can_read(request,"project")


class GoalForm(forms.ModelForm):
    class Meta:
        model=ImpactGoal
        fields=["title","module","program","description","unit","baseline","target","direction","start_date","due_date","owner"]
        widgets={"start_date":forms.DateInput(attrs={"type":"date"}),"due_date":forms.DateInput(attrs={"type":"date"})}

    def __init__(self,*args,request,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["module"].choices=modules(request)
        self.fields["program"].queryset=scoped(request,TrainingProgram) if can_read(request,"training-program") else TrainingProgram.objects.none()
        self.fields["owner"].queryset=get_user_model().objects.filter(memberships__company=request.company,memberships__is_active=True,memberships__role__in=["owner","admin","user"],is_active=True).distinct()
        if self.instance.pk and self.instance.measurements.exists():
            for key in ("module","program","unit","baseline","target","direction","start_date","due_date"):
                self.fields[key].disabled=True
                self.fields[key].help_text="ثابت بعد تسجيل القياسات؛ أنشئ مؤشرًا جديدًا عند تغيير خطة القياس."
        style_form(self)


class MeasurementForm(forms.ModelForm):
    evidence_file=forms.FileField(label="مستند إثبات (اختياري)",required=False,help_text="PDF أو صورة PNG/JPG/WebP، حتى 5 ميجابايت.")
    class Meta:
        model=ImpactMeasurement
        fields=["measured_on","value","source"]
        widgets={"measured_on":forms.DateInput(attrs={"type":"date"})}

    def clean_evidence_file(self):
        upload=self.cleaned_data.get("evidence_file")
        if not upload:
            return None
        if upload.size>5*1024*1024:
            raise ValidationError("حجم المستند يتجاوز 5 ميجابايت.")
        data=upload.read()
        valid=data.startswith((b"%PDF-",b"\x89PNG\r\n\x1a\n",b"\xff\xd8\xff")) or data.startswith(b"RIFF") and data[8:12]==b"WEBP"
        if not valid:
            raise ValidationError("صيغة الإثبات غير مدعومة.")
        return (upload.name[:255],data)


def metric(goal,measurement,today):
    actual=measurement.value if measurement else None
    progress=flt((actual-goal.baseline)*100/(goal.target-goal.baseline),1) if actual is not None and goal.target!=goal.baseline else None
    return {"goal":goal,"measurement":measurement,"actual":actual,"progress":progress,"bar":max(0,min(progress or 0,100)),"overdue":goal.due_date<today and (progress is None or progress<100),"achieved":progress is not None and progress>=100}


def rows_for(request,as_of):
    approved=ImpactMeasurement.objects.filter(company=request.company,goal_id=OuterRef("pk"),status="approved",measured_on__lte=as_of,reviewed_at__date__lte=as_of).order_by("-measured_on","-pk")
    rows=list(goals(request).filter(created_at__date__lte=as_of).annotate(latest_id=Subquery(approved.values("pk")[:1])))
    latest={m.pk:m for m in ImpactMeasurement.objects.filter(company=request.company,pk__in=[g.latest_id for g in rows if g.latest_id]).defer("evidence")}
    return [metric(g,latest.get(g.latest_id),as_of) for g in rows]


def program_costs(request,as_of):
    if not can_read(request,"training-program") or not cost_access(request):
        return []
    from accounts.models import GLEntry, RootType
    links=list(ProgramCostLink.objects.filter(company=request.company,program__start_date__lte=as_of).exclude(program__status=ProgramStatus.CANCELLED).select_related("program","project"))
    sums=GLEntry.objects.filter(company=request.company,project_id__in=[l.project_id for l in links],posting_date__lte=as_of,is_cancelled=False,account__root_type=RootType.EXPENSE).values("project_id").annotate(debit=Sum("debit"),credit=Sum("credit"))
    by_project={r["project_id"]:flt(r["debit"]-r["credit"]) for r in sums}
    # The denominator uses current admitted status and excludes registrations after the cost cut-off.
    counts={r["program_id"]:r["total"] for r in ProgramEnrollment.objects.filter(company=request.company,program_id__in=[l.program_id for l in links],status__in=ADMITTED_STATUSES,registered_at__date__lte=as_of).values("program_id").annotate(total=Count("pk"))}
    result=[]
    for link in links:
        enrolled=counts.get(link.program_id,0)
        actual=by_project.get(link.project_id,Decimal(0))
        result.append({"program":link.program,"project":link.project,"budget":link.project.cost_budget,"actual":actual,"variance":flt(link.project.cost_budget-actual),"participants":enrolled,"per_person":flt(actual/enrolled) if enrolled else None})
    return result


@login_required
def center(request):
    require_access(request)
    today=timezone.localdate()
    try:
        as_of=datetime.date.fromisoformat(request.GET.get("as_of",str(today)))
        if as_of>today:
            raise ValueError
    except ValueError:
        as_of=today
        messages.error(request,"اختر تاريخًا صحيحًا لا يتجاوز اليوم.")
    rows=rows_for(request,as_of)
    pending=ImpactMeasurement.objects.filter(company=request.company,goal_id__in=[r["goal"].pk for r in rows],status="pending").count()
    costs=program_costs(request,as_of)
    if request.GET.get("format")=="csv":
        if not request.membership.may("export"):
            raise PermissionDenied
        response=HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"]='attachment; filename="impact-results.csv"'
        response.write("\ufeff")
        writer=csv.writer(response)
        writer.writerow(["المؤشر","الوحدة","خط الأساس","المستهدف","آخر قياس معتمد","نسبة تحقيق المستهدف","تاريخ القياس","الموعد المستهدف"])
        for row in rows:
            g=row["goal"]
            writer.writerow([csv_safe(v) for v in [g.title,g.unit,g.baseline,g.target,row["actual"],row["progress"],row["measurement"].measured_on if row["measurement"] else "",g.due_date]])
        return response
    return render(request,"programs/impact_center.html",{"rows":rows,"costs":costs,"as_of":as_of,"pending":pending,"achieved":sum(r["achieved"] for r in rows),"overdue":sum(r["overdue"] for r in rows),"show_costs":cost_access(request) and can_read(request,"training-program"),"page":Paginator(rows,20).get_page(request.GET.get("page"))})


@login_required
def goal_form(request,pk=None):
    require_access(request)
    if not request.membership.can_manage:
        raise PermissionDenied
    with transaction.atomic():
        obj=get_object_or_404(goals(request).select_for_update(of=("self",)),pk=pk) if pk else ImpactGoal(company=request.company)
        form=GoalForm(request.POST if request.method=="POST" else None,instance=obj,request=request)
        if request.method=="POST" and form.is_valid():
            owner=Membership.objects.filter(company=request.company,user=form.cleaned_data["owner"],is_active=True).select_related("role_policy").first()
            if not owner or not owner.allows_module(form.cleaned_data["module"]) or (form.cleaned_data["module"]=="programs" and owner.doctype_access("training-program")=="none"):
                form.add_error("owner","المسؤول لا يملك صلاحية الوحدة المختارة.")
            else:
                form.save()
                return redirect(obj.get_absolute_url())
    return render(request,"programs/impact_form.html",{"form":form,"title":"تعريف مؤشر أثر"})


@login_required
def detail(request,pk):
    require_access(request)
    with transaction.atomic():
        goal=get_object_or_404(goals(request).select_for_update(of=("self",)),pk=pk)
        form=MeasurementForm(request.POST if request.method=="POST" else None,request.FILES if request.method=="POST" else None,instance=ImpactMeasurement(company=request.company,goal=goal,submitted_by=request.user))
        style_form(form)
        if request.method=="POST" and form.is_valid():
            measurement=form.save(commit=False)
            upload=form.cleaned_data.get("evidence_file")
            if upload:
                measurement.file_name,measurement.evidence=upload
            measurement.save()
            messages.success(request,"سُجل القياس بانتظار المراجعة؛ يظهر في النتائج بعد اعتماده.")
            return redirect(goal.get_absolute_url())
    records=goal.measurements.filter(company=request.company).defer("evidence").select_related("submitted_by","reviewed_by")
    latest=records.filter(status="approved",measured_on__lte=timezone.localdate()).first()
    return render(request,"programs/impact_goal.html",{"goal":goal,"form":form,"records":Paginator(records,20).get_page(request.GET.get("page")),"metric":metric(goal,latest,timezone.localdate())})


@login_required
@require_POST
def review(request,pk):
    if not request.membership.can_manage:
        raise PermissionDenied
    with transaction.atomic():
        row=get_object_or_404(ImpactMeasurement.objects.select_for_update(),pk=pk,company=request.company,goal__in=goals(request))
        decision=request.POST.get("decision")
        note=request.POST.get("note","").strip()[:1000]
        if row.status!="pending":
            messages.error(request,"تمت مراجعة هذا القياس من قبل.")
        elif decision not in ("approve","reject") or decision=="reject" and not note:
            messages.error(request,"حدد القرار واكتب سبب الرفض عند الرفض.")
        else:
            row.status="approved" if decision=="approve" else "rejected"
            row.reviewed_by=request.user;row.reviewed_at=timezone.now();row.review_note=note
            row.save(update_fields=["status","reviewed_by","reviewed_at","review_note"])
    return redirect(row.goal.get_absolute_url())


@login_required
def evidence(request,pk):
    row=get_object_or_404(ImpactMeasurement,pk=pk,company=request.company,goal__in=goals(request))
    if not row.file_name:
        from django.http import Http404
        raise Http404
    response=FileResponse(BytesIO(bytes(row.evidence)),as_attachment=True,filename=row.file_name,content_type="application/octet-stream")
    response["X-Content-Type-Options"]="nosniff"
    return response


class CostForm(forms.Form):
    confirm=forms.BooleanField(label="تحققت أن المشروع مخصص لهذا البرنامج وحده",required=True)
    project=forms.ModelChoiceField(queryset=None,label="مشروع محاسبي مخصص للبرنامج",help_text="تُحسب مصروفات المشروع كاملة؛ لا تستخدم مشروعًا مشتركًا بين برامج مختلفة.")
    def __init__(self,*args,request,**kwargs):
        super().__init__(*args,**kwargs)
        from projects.models import Project
        self.fields["project"].queryset=scoped(request,Project).exclude(training_cost_link__isnull=False)
        style_form(self)


@login_required
def cost_link(request,pk):
    if not request.membership.can_manage or not cost_access(request) or not can_read(request,"training-program"):
        raise PermissionDenied
    program=get_object_or_404(scoped(request,TrainingProgram),pk=pk)
    existing=ProgramCostLink.objects.filter(company=request.company,program=program).select_related("project").first()
    form=CostForm(request.POST if request.method=="POST" else None,request=request)
    if request.method=="POST" and not existing and form.is_valid():
        try:
            with transaction.atomic():
                ProgramCostLink.objects.create(company=request.company,program=program,project=form.cleaned_data["project"],linked_by=request.user)
            return redirect("impact_center")
        except IntegrityError:
            form.add_error(None,"ارتبط البرنامج أو المشروع بالفعل؛ أعد فتح الصفحة.")
    return render(request,"programs/impact_form.html",{"form":form,"title":f"ربط تكلفة برنامج: {program.title}","existing":existing})
