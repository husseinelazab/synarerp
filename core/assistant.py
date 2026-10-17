"""Permission-filtered numerical context, with an optional explicitly enabled LLM."""
import json
import re
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied,ValidationError
from django.shortcuts import redirect,render
from django.urls import reverse
from django.utils import timezone
from django.db.models import Sum
from core.forms import style_form
from core.models import AssistantSettings,AssistantMessage
from core.mail import encrypt_password,decrypt_password
from core.workspace import can_read,can_module,scoped


class SettingsForm(forms.ModelForm):
    api_key=forms.CharField(label="مفتاح Anthropic",required=False,widget=forms.PasswordInput(render_value=False))
    clear_key=forms.BooleanField(label="إزالة المفتاح",required=False)
    class Meta:
        model=AssistantSettings
        fields=["enabled","model","consent"]

    def clean(self):
        data=super().clean()
        key=data.get("api_key","").strip()
        if key and not key.startswith("sk-ant-"):
            self.add_error("api_key","أدخل مفتاح Anthropic صالحًا من حساب الجهة.")
        if data.get("model") and not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}",data["model"]):
            self.add_error("model","اسم النموذج غير صالح.")
        has_key=bool(key or self.instance.api_key_encrypted and not data.get("clear_key"))
        if data.get("enabled") and (not has_key or not data.get("consent")):
            raise ValidationError("التفعيل يتطلب مفتاح الجهة والموافقة على إرسال السؤال وملخص الأرقام.")
        return data


TOPICS=[("work","ماذا ينتظرني في العمل؟"),("impact","ملخص الأثر والنتائج"),("sales","ملخص مبيعات اليوم"),("stock","متابعة نقص المخزون"),("letter","مساعدة في صياغة خطاب")]


def choices(request):
    result=[TOPICS[0]]
    from programs.impact_center import modules
    if modules(request):result.append(TOPICS[1])
    if can_read(request,"sales-invoice"):result.append(TOPICS[2])
    if can_module(request,"stock") and can_read(request,"item") and can_read(request,"warehouse"):result.append(TOPICS[3])
    if can_read(request,"correspondence"):result.append(TOPICS[4])
    return result


class AskForm(forms.Form):
    topic=forms.ChoiceField(label="الموضوع")
    question=forms.CharField(label="سؤالك أو تعليمات الصياغة (اختياري)",required=False,max_length=2000,widget=forms.Textarea(attrs={"rows":3}))
    use_provider=forms.BooleanField(label="استخدام مزود الذكاء الاصطناعي للسؤال وملخص الأرقام",required=False)
    def __init__(self,*args,request,**kwargs):
        super().__init__(*args,**kwargs);self.fields["topic"].choices=choices(request);style_form(self)


def snapshot(request,topic):
    today=timezone.localdate();facts={};sources=[]
    if topic=="work":
        from core.processes import pending_cases
        from beneficiaries.models import BeneficiaryFollowUp
        facts["مراحل تنتظر إجرائي"]=pending_cases(request).count()
        if can_read(request,"beneficiary-follow-up"):
            facts["متابعات مسندة مفتوحة"]=scoped(request,BeneficiaryFollowUp).filter(assigned_to=request.user,completed=False).count()
        sources=[{"label":"مركز عملي","url":reverse("work_center")}]
    elif topic=="impact":
        from programs.impact_center import rows_for
        rows=rows_for(request,today)
        facts={"المؤشرات المتاحة":len(rows),"المؤشرات التي حققت المستهدف":sum(r["achieved"] for r in rows),"المؤشرات المتأخرة":sum(r["overdue"] for r in rows)}
        sources=[{"label":"لوحة الأثر والنتائج","url":reverse("impact_center")}]
    elif topic=="sales":
        from selling.models import SalesInvoice
        qs=scoped(request,SalesInvoice).filter(docstatus=1,posting_date=today)
        totals=qs.aggregate(amount=Sum("base_grand_total"))
        facts={"المستندات المعتمدة اليوم":qs.count(),"صافي الفواتير والمرتجعات بعملة الجهة":str(totals["amount"] or 0)}
        sources=[{"label":"فواتير المبيعات","url":reverse("doc_list",args=["sales-invoice"])}]
    elif topic=="stock":
        from stock.models import Bin
        qs=scoped(request,Bin)
        facts={"أرصدة أصناف سالبة في المستودعات":qs.filter(actual_qty__lt=0).count()}
        sources=[{"label":"متابعة الأرصدة السالبة","url":"/reports/stock/balance/?negative=1"}]
    elif topic=="letter":
        facts={"طريقة الاستخدام":"اكتب الغرض والجهة المستلمة في السؤال لصياغة مسودة. راجع الصياغة قبل نسخها إلى معاملة صادرة."}
        sources=[{"label":"معاملة صادرة جديدة","url":reverse("doc_new",args=["correspondence"])+"?direction=outgoing"}]
    return facts,sources


def generate(config,question,facts):
    from buying.ai import _client
    client=_client(decrypt_password(config.api_key_encrypted))
    response=client.messages.create(model=config.model,max_tokens=1500,system="أنت مساعد عربي لنظام ERP. استخدم فقط الأرقام المصرح بها في الملخص. لا تخترع أرقامًا أو مصادر. بيّن نقص البيانات. لا تنفذ إجراءات ولا تدع تنفيذها. للصياغة اكتب مسودة قابلة للمراجعة، ولا تفترض أسماء أو تواريخ غير معطاة.",messages=[{"role":"user","content":json.dumps({"question":question,"authorized_summary":facts},ensure_ascii=False)}])
    text="\n".join(block.text for block in response.content if getattr(block,"type","")=="text")
    if not text.strip():raise ValueError("empty provider answer")
    return text[:12000]


@login_required
def settings_page(request):
    if not request.membership.can_manage:raise PermissionDenied
    obj,_=AssistantSettings.objects.get_or_create(company=request.company)
    form=SettingsForm(request.POST if request.method=="POST" else None,instance=obj);style_form(form)
    if request.method=="POST" and form.is_valid():
        obj=form.save(commit=False)
        if form.cleaned_data.get("clear_key"):obj.api_key_encrypted=""
        if form.cleaned_data.get("api_key"):obj.api_key_encrypted=encrypt_password(form.cleaned_data["api_key"].strip())
        obj.save();messages.success(request,"حُفظ إعداد المزود. الملخص المحلي يظل متاحًا دون اتصال بمزود خارجي.")
        return redirect("assistant_settings")
    return render(request,"settings/assistant.html",{"form":form,"has_key":bool(obj.api_key_encrypted)})


@login_required
def chat(request):
    form=AskForm(request.POST if request.method=="POST" else None,request=request)
    message=None;facts={}
    config=AssistantSettings.objects.filter(company=request.company,enabled=True,consent=True).exclude(api_key_encrypted="").first()
    subscription=getattr(request.company,"subscription",None)
    provider_available=bool(config and subscription and subscription.plan.includes_ai)
    if request.method=="POST" and form.is_valid():
        key=f"assistant:{request.company.pk}:{request.user.pk}"
        if (cache.get(key) or 0)>=30:
            form.add_error(None,"بلغت حد الأسئلة المؤقت. أعد المحاولة لاحقًا.")
        elif form.cleaned_data["use_provider"] and not provider_available:
            form.add_error(None,"استخدام المزود يحتاج تفعيله من المدير وباقة تتيح الذكاء الاصطناعي.")
        else:
            topic=form.cleaned_data["topic"]
            facts,sources=snapshot(request,topic)
            answer="\n".join(f"{label}: {value}" for label,value in facts.items())
            used=False
            if form.cleaned_data["use_provider"]:
                try:
                    answer=generate(config,form.cleaned_data["question"] or dict(TOPICS)[topic],facts);used=True
                except Exception:
                    form.add_error(None,"تعذر الحصول على إجابة من المزود. راجع المفتاح واسم النموذج؛ لم تُنفذ أي تغييرات.")
            if not form.errors:
                cache.set(key,(cache.get(key) or 0)+1,3600)
                message=AssistantMessage.objects.create(company=request.company,user=request.user,topic=topic,question=form.cleaned_data["question"],answer=answer,sources=sources,provider_used=used)
    # No past answers are exposed after permissions change; sources are regenerated on demand.
    return render(request,"core/assistant.html",{"form":form,"message":message,"facts":facts,"provider_available":provider_available})
