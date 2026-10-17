"""Versioned, sequential processes with immutable per-request stage snapshots."""
import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.forms import inlineformset_factory
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.forms import style_form
from core.models import Membership, Notification, ProcessTemplate, ProcessStage, ProcessCase, ProcessEvent
from core.permissions import company_modules
from core.workspace import can_module, can_read, scoped


class TemplateForm(forms.ModelForm):
    class Meta:
        model = ProcessTemplate
        fields = ["name", "module", "description", "enabled"]

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["module"] = forms.ChoiceField(label="الوحدة", choices=[(k,v) for k,v in company_modules(request.company) if can_module(request,k)])
        style_form(self)


class StageForm(forms.ModelForm):
    class Meta:
        model = ProcessStage
        fields = ["position", "name", "assignee", "role", "days", "min_amount"]

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = get_user_model().objects.filter(memberships__company=request.company,memberships__is_active=True,memberships__role__in=["owner","admin","user"],is_active=True).distinct()
        style_form(self)

    def clean(self):
        data = super().clean()
        if data.get("DELETE"):
            return data
        if bool(data.get("assignee")) == bool(data.get("role")):
            raise ValidationError("حدد موظفًا أو دورًا مسؤولًا واحدًا لكل مرحلة.")
        if data.get("days") is not None and not 1 <= data["days"] <= 365:
            self.add_error("days","مدة المرحلة بين يوم و365 يومًا.")
        if data.get("min_amount",0) is not None and data.get("min_amount",0) < 0:
            self.add_error("min_amount","القيمة لا يمكن أن تكون سالبة.")
        return data


StageSet = inlineformset_factory(ProcessTemplate,ProcessStage,form=StageForm,extra=1,can_delete=True,min_num=1,validate_min=True,max_num=30,validate_max=True)


class CaseForm(forms.ModelForm):
    class Meta:
        model = ProcessCase
        fields = ["title","description","amount","beneficiary"]
        labels = {"beneficiary":"ربط بملف مستفيد (اختياري)"}

    def __init__(self,*args,request,**kwargs):
        super().__init__(*args,**kwargs)
        if not request.company.is_nonprofit or not can_read(request,"beneficiary"):
            self.fields.pop("beneficiary")
        else:
            from beneficiaries.models import Beneficiary
            self.fields["beneficiary"].queryset=scoped(request,Beneficiary)
        self.fields["amount"].min_value=0
        style_form(self)

    def clean_amount(self):
        value=self.cleaned_data["amount"]
        if value < 0:
            raise ValidationError("القيمة لا يمكن أن تكون سالبة.")
        return value


def responsible(case,member):
    return (case.assigned_to_id == member.user_id or
            case.assigned_role == "manager" and member.can_manage or
            case.assigned_role == "owner" and member.role == "owner")


def visible_cases(request):
    qs=ProcessCase.objects.filter(company=request.company).select_related("template","created_by","assigned_to","beneficiary")
    if not request.membership.can_manage:
        qs=qs.filter(Q(created_by=request.user)|Q(assigned_to=request.user)|Q(events__actor=request.user)).distinct()
    modules=[key for key,label in company_modules(request.company) if can_module(request,key)]
    return qs.filter(module__in=modules)


def pending_cases(request):
    assigned=Q(assigned_to=request.user)
    if request.membership.can_manage:
        assigned |= Q(assigned_role="manager")
    if request.membership.role=="owner":
        assigned |= Q(assigned_role="owner")
    return visible_cases(request).filter(assigned,status="active")


@login_required
def case_list(request):
    qs=visible_cases(request)
    query=request.GET.get("q","").strip()[:200]
    status=request.GET.get("status","")
    if status in dict(ProcessCase._meta.get_field("status").choices):
        qs=qs.filter(status=status)
    if query:
        cond=Q(title__icontains=query)|Q(description__icontains=query)
        if query.isdigit():
            cond |= Q(pk=int(query))
        qs=qs.filter(cond)
    return render(request,"work/list.html",{"page":Paginator(qs,25).get_page(request.GET.get("page")),"query":query,"selected_status":status,"statuses":ProcessCase._meta.get_field("status").choices})


def advance(case):
    if case.current_step >= len(case.snapshot):
        case.status="done";case.assigned_to=None;case.assigned_role="";case.due_date=None
    else:
        step=case.snapshot[case.current_step]
        case.assigned_to_id=step["assignee"];case.assigned_role=step["role"]
        case.due_date=timezone.localdate()+datetime.timedelta(days=step["days"])
    case.save()
    if case.status == "done":
        Notification.objects.create(company=case.company,user_id=case.created_by_id,title=f"اكتمل: {case.title}"[:255],url=case.get_absolute_url())
    if case.status == "active":
        recipients=Membership.objects.filter(company=case.company,is_active=True,user__is_active=True).select_related("role_policy")
        recipients=[m for m in recipients if responsible(case,m) and m.may_submit and m.allows_module(case.module)]
        Notification.objects.bulk_create([Notification(company=case.company,user_id=m.user_id,title=f"بانتظارك: {case.title} — {case.step_name}"[:255],url=case.get_absolute_url()) for m in recipients])


@login_required
def templates(request):
    rows=ProcessTemplate.objects.filter(company=request.company)
    rows=[r for r in rows if can_module(request,r.module) and (r.enabled or request.membership.can_manage)]
    return render(request,"work/templates.html",{"rows":rows})


@login_required
def builder(request,pk=None):
    if not request.membership.can_manage:
        raise PermissionDenied
    obj=get_object_or_404(ProcessTemplate,pk=pk,company=request.company) if pk else ProcessTemplate(company=request.company)
    form=TemplateForm(request.POST if request.method=="POST" else None,instance=obj,request=request)
    formset=StageSet(request.POST if request.method=="POST" else None,instance=obj,form_kwargs={"request":request})
    if request.method=="POST" and form.is_valid() and formset.is_valid():
        rows=[f.cleaned_data for f in formset if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        positions=[r["position"] for r in rows]
        if len(positions)!=len(set(positions)):
            form.add_error(None,"استخدم ترتيبًا مختلفًا لكل مرحلة.")
        elif not any(r["min_amount"]==0 for r in rows):
            form.add_error(None,"أضف مرحلة واحدة على الأقل بلا حد مالي حتى لا يكتمل طلب دون مراجعة.")
        else:
            members={m.user_id:m for m in Membership.objects.filter(company=request.company,is_active=True).select_related("role_policy")}
            for row in rows:
                member=members.get(row["assignee"].pk) if row.get("assignee") else None
                if member and (not member.may_submit or not member.allows_module(form.cleaned_data["module"])):
                    form.add_error(None,"مسؤول المرحلة يجب أن يملك صلاحية الاعتماد والوحدة المختارة.")
            if form.errors:
                return render(request,"work/builder.html",{"form":form,"formset":formset,"obj":obj})
            with transaction.atomic():
                if obj.pk:
                    ProcessTemplate.objects.select_for_update().get(pk=obj.pk,company=request.company)
                form.save();formset.save()
            messages.success(request,"حُفظ الإجراء. الطلبات السابقة تحتفظ بمراحلها الأصلية.")
            return redirect("process_templates")
    return render(request,"work/builder.html",{"form":form,"formset":formset,"obj":obj})


@login_required
def start(request,pk):
    obj=get_object_or_404(ProcessTemplate,pk=pk,company=request.company,enabled=True)
    if not can_module(request,obj.module):
        raise PermissionDenied
    form=CaseForm(request.POST if request.method=="POST" else None,request=request)
    if request.method=="POST" and form.is_valid():
        try:
            with transaction.atomic():
                obj=get_object_or_404(ProcessTemplate.objects.select_for_update(),pk=obj.pk,company=request.company,enabled=True)
                stages=list(obj.stages.order_by("position","pk"))
                active_users={m.user_id for m in Membership.objects.filter(company=request.company,is_active=True,user__is_active=True,role__in=["owner","admin","user"]).select_related("role_policy") if m.may_submit and m.allows_module(obj.module)}
                snapshot=[{"name":s.name,"assignee":s.assignee_id,"role":s.role,"days":s.days} for s in stages if s.min_amount<=form.cleaned_data["amount"]]
                if not snapshot or any(s["assignee"] and s["assignee"] not in active_users for s in snapshot):
                    raise ValidationError("راجع مراحل الإجراء؛ يلزم وجود مراحل ومسؤولون نشطون داخل الجهة.")
                case=form.save(commit=False);case.company=request.company;case.template=obj;case.module=obj.module
                case.created_by=request.user;case.snapshot=snapshot;advance(case)
                ProcessEvent.objects.create(company=request.company,case=case,actor=request.user,action="start")
            return redirect(case.get_absolute_url())
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,"work/start.html",{"form":form,"template":obj})


@login_required
def detail(request,pk):
    case=get_object_or_404(visible_cases(request),pk=pk)
    return render(request,"work/detail.html",{"case":case,"events":case.events.select_related("actor"),"can_beneficiary":can_read(request,"beneficiary"),"can_act":case.status=="active" and responsible(case,request.membership) and request.membership.may_submit,"can_cancel":case.status=="active" and (case.created_by_id==request.user.pk or request.membership.can_manage)})


@login_required
@require_POST
def act(request,pk):
    with transaction.atomic():
        # Lock the underlying row, avoiding nullable joins on PostgreSQL.
        case=get_object_or_404(ProcessCase.objects.select_for_update(),pk=pk,company=request.company)
        if not visible_cases(request).filter(pk=pk).exists():
            raise PermissionDenied
        action=request.POST.get("action")
        if case.status!="active" or str(case.version)!=request.POST.get("version"):
            messages.error(request,"تغير الطلب أو انتهى بالفعل؛ راجع الحالة الحالية.")
            return redirect(case.get_absolute_url())
        note=request.POST.get("note","").strip()[:2000]
        if action=="cancel":
            if case.created_by_id!=request.user.pk and not request.membership.can_manage:
                raise PermissionDenied
            case.status="cancelled"
        elif action in ("approve","reject"):
            if not responsible(case,request.membership) or not request.membership.may_submit:
                raise PermissionDenied
            if action=="reject" and not note:
                messages.error(request,"اكتب سبب الرفض.")
                return redirect(case.get_absolute_url())
            if action=="reject":
                case.status="rejected"
            else:
                case.current_step+=1
        else:
            raise PermissionDenied
        ProcessEvent.objects.create(company=request.company,case=case,actor=request.user,stage_name=case.step_name if action!="approve" else case.snapshot[case.current_step-1]["name"],action=action,note=note)
        if case.status in ("rejected","cancelled") and case.created_by_id != request.user.pk:
            Notification.objects.create(company=case.company,user_id=case.created_by_id,title=f"{case.get_status_display()}: {case.title}"[:255],url=case.get_absolute_url())
        case.version+=1
        if case.status=="active":
            advance(case)
        else:
            case.assigned_to=None;case.assigned_role="";case.due_date=None;case.save()
    messages.success(request,"تم تسجيل الإجراء وتحديث سجل الطلب.")
    return redirect(case.get_absolute_url())
