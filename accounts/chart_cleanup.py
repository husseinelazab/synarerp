from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from accounts.models import Account
from core.models import Company


def candidates(company):
    rows = list(Account.objects.filter(company=company).order_by("pk"))
    parents = {row.pk:row.parent_id for row in rows}
    protected = set()
    for relation in Account._meta.get_fields(include_hidden=True):
        if not relation.auto_created or relation.related_model is Account or not (relation.one_to_many or relation.one_to_one):
            continue
        field = relation.field
        protected.update(relation.related_model._base_manager.filter(**{field.name+"__company_id":company.pk})
                         .values_list(field.attname,flat=True))
    stack = list(protected)
    while stack:
        parent = parents.get(stack.pop())
        if parent and parent not in protected:
            protected.add(parent)
            stack.append(parent)
    return [row for row in rows if row.pk not in protected], protected


@login_required
def cleanup(request):
    if not request.membership.can_manage or request.company.is_nonprofit:
        raise PermissionDenied
    if request.method == "POST":
        try:
            payload = signing.loads(request.POST.get("preview",""), salt="chart-cleanup",max_age=900)
        except signing.BadSignature:
            messages.error(request,"انتهت المعاينة؛ افتح الصفحة مجدداً قبل الحذف.")
            return redirect("chart_cleanup")
        if payload.get("company") != request.company.pk or request.POST.get("confirm") != "on":
            raise PermissionDenied
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=request.company.pk)
            list(Account.objects.select_for_update().filter(company=request.company))
            rows, _ = candidates(request.company)
            allowed = {row.pk for row in rows}
            ids = set(payload["ids"])
            if not ids <= allowed:
                messages.error(request,"تغيرت ارتباطات الحسابات منذ المعاينة. راجع القائمة الجديدة.")
                return redirect("chart_cleanup")
            pending = {r.pk:r for r in rows if r.pk in ids}
            deleted = len(pending)
            while pending:
                parents = {r.parent_id for r in pending.values()}
                leaves = [r for pk,r in pending.items() if pk not in parents]
                if not leaves:
                    raise ValueError("دورة غير صالحة في دليل الحسابات")
                for row in leaves:
                    pk = row.pk
                    row.delete()
                    del pending[pk]
        messages.success(request,f"حُذف {deleted} حساباً غير مستخدم. احتُفظ بالحسابات المرتبطة بالإعدادات أو المستندات.")
        return redirect("chart_of_accounts")
    rows, protected = candidates(request.company)
    token = signing.dumps({"company":request.company.pk,"ids":[r.pk for r in rows]},salt="chart-cleanup")
    return render(request,"accounts/chart_cleanup.html",{"rows":rows,"protected_count":len(protected),"preview":token})
