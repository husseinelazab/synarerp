from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from collections import defaultdict

from core.approvals import document_amount
from core.models import ApprovalRule, ApprovalRecord, Notification
from core.processes import visible_cases, pending_cases
from core.registry import REGISTRY
from core.workspace import can_read, scoped
from core.permissions import path_allowed


@login_required
def center(request):
    today=timezone.localdate()
    tasks=[]
    def add(slug,qs,title_field,due_field):
        if not can_read(request,slug):
            return
        for row in qs.order_by(due_field,"pk")[:100]:
            due=getattr(row,due_field)
            tasks.append({"title":str(getattr(row,title_field)),"kind":str(row._meta.verbose_name),"due":due,"overdue":bool(due and due<today),"url":row.get_absolute_url()})
    from projects.models import ProjectTask
    from office.models import Correspondence
    from beneficiaries.models import BeneficiaryFollowUp
    if can_read(request,"project-task"):
        add("project-task",scoped(request,ProjectTask).filter(assigned_to=request.user,status__in=["open","working"]),"title","due_date")
    if can_read(request,"correspondence"):
        add("correspondence",scoped(request,Correspondence).filter(assigned_to=request.user).exclude(status="closed"),"subject","due_date")
    if can_read(request,"beneficiary-follow-up"):
        add("beneficiary-follow-up",scoped(request,BeneficiaryFollowUp).filter(assigned_to=request.user,completed=False),"subject","due_date")
    cases=list(visible_cases(request)[:30])
    pending=list(pending_cases(request)[:200])
    approvals=[]
    if request.membership.may_submit:
        rules_by_type=defaultdict(list)
        for rule in ApprovalRule.objects.filter(company=request.company,disabled=False).order_by("doctype","level"):
            rules_by_type[rule.doctype].append(rule)
        for slug,rules in rules_by_type.items():
            dt=REGISTRY.get(slug)
            if not dt or not dt.is_submittable or not can_read(request,slug):
                continue
            documents=list(scoped(request,dt.model).filter(docstatus=0).order_by("pk")[:200])
            completed=defaultdict(set)
            for object_id,level in ApprovalRecord.objects.filter(company=request.company,doctype=slug,object_id__in=[d.pk for d in documents]).values_list("object_id","level"):
                completed[object_id].add(level)
            for doc in documents:
                amount=document_amount(doc)
                waiting=[r for r in rules if r.min_amount<=amount and r.level not in completed[doc.pk]]
                next_rules=[r for r in waiting if r.level==waiting[0].level] if waiting else []
                if any(r.can_approve(request.membership) for r in next_rules) and path_allowed(request.membership,f"/app/{slug}/{doc.pk}/submit/"):
                    due=getattr(doc,"needed_by",None) or getattr(doc,"due_date",None)
                    approvals.append({"title":str(doc),"kind":str(dt.label),"url":doc.get_absolute_url(),"due":due,"overdue":bool(due and due<today)})
    tasks.sort(key=lambda r:(not r["overdue"],r["due"] or today,r["title"]))
    safe_urls={row["url"] for row in tasks+approvals} | {c.get_absolute_url() for c in cases+pending}
    notes=[n for n in Notification.objects.filter(company=request.company,user=request.user,is_read=False)[:100] if n.url in safe_urls][:10]
    return render(request,"work/center.html",{"tasks":tasks,"pending":pending,"cases":cases[:30],"approvals":approvals,"notifications":notes,"overdue_count":sum(t["overdue"] for t in tasks+approvals)+sum(bool(c.overdue) for c in pending)})
