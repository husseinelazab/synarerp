"""Validated stationery controls and a sample preview, without arbitrary CSS/HTML."""
from types import SimpleNamespace
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils import timezone
from core.forms import style_form
from core.models import PrintSettings
from stock.barcode import svg


class StudioForm(forms.Form):
    kind=forms.ChoiceField(label="نوع المستند",choices=[("letter","الخطابات الرسمية"),("certificate","الشهادات"),("reports","الفواتير والتقارير المطبوعة")],initial="letter",required=False)

    def clean_kind(self):return self.cleaned_data.get("kind") or "letter"

    margin=forms.IntegerField(label="هامش الصفحة (مم)",min_value=10,max_value=25,initial=14)
    logo_width=forms.IntegerField(label="عرض الشعار (مم)",min_value=25,max_value=70,initial=58)
    card_width=forms.IntegerField(label="عرض مربع الصادر (مم)",min_value=55,max_value=85,initial=68)
    font_size=forms.IntegerField(label="حجم نص الخطاب",min_value=12,max_value=20,initial=15)
    line_height=forms.DecimalField(label="تباعد الأسطر",min_value=1.4,max_value=2.5,decimal_places=1,initial=2)
    identity_side=forms.ChoiceField(label="موضع هوية الجمعية",choices=[("right","يمين"),("left","يسار")],initial="right")
    signature_side=forms.ChoiceField(label="موضع التوقيع",choices=[("left","يسار"),("center","وسط"),("right","يمين")],initial="left")


def layout(settings,kind="letter"):
    defaults={name:field.initial for name,field in StudioForm.base_fields.items()}
    raw=getattr(settings,"document_layout",{})
    if not isinstance(raw,dict):raw={}
    chosen=raw if kind=="letter" else raw.get("layouts",{}).get(kind,{})
    if kind=="reports":defaults.update(margin=12,font_size=13)
    if kind=="certificate":defaults.update(margin=20,font_size=18)
    defaults["kind"]=kind
    form=StudioForm(data={**defaults,**chosen})
    result={key:str(value) for key,value in (form.cleaned_data if form.is_valid() else defaults).items()}
    result["configured"]=bool(chosen)
    return result


@login_required
def studio(request):
    if not request.membership.can_manage:
        raise PermissionDenied
    ps=PrintSettings.for_company(request.company)
    kind=request.GET.get("kind","letter")
    if kind not in ("letter","certificate","reports"):kind="letter"
    form=StudioForm(request.POST if request.method=="POST" else None,initial=layout(ps,kind))
    style_form(form)
    if request.method=="POST" and form.is_valid():
        kind=form.cleaned_data["kind"]
        data={k:str(v) for k,v in form.cleaned_data.items() if k!="kind"}
        if kind=="letter":ps.document_layout={**ps.document_layout,**data}
        else:ps.document_layout={**ps.document_layout,"layouts":{**ps.document_layout.get("layouts",{}),kind:data}}
        if ps.pk:ps.save(update_fields=["document_layout"])
        else:ps.save()
        messages.success(request,"حُفظ تصميم الخطابات لهذه الجهة.")
        return redirect("document_studio")
    return render(request,"settings/document_studio.html",{"form":form})


@login_required
@xframe_options_sameorigin
def preview(request):
    if not request.membership.can_manage:
        raise PermissionDenied
    ps=PrintSettings.for_company(request.company)
    kind=request.GET.get("kind","letter")
    if kind not in ("letter","certificate","reports"):kind="letter"
    form=StudioForm(request.GET or layout(ps,kind))
    if form.is_valid():
        data={k:str(v) for k,v in form.cleaned_data.items() if k!="kind"}
        if kind=="letter":ps.document_layout={**ps.document_layout,**data}
        else:ps.document_layout={**ps.document_layout,"layouts":{**ps.document_layout.get("layouts",{}),kind:data}}
    if kind=="certificate":
        program=SimpleNamespace(title="برنامج تجريبي",get_program_type_display="دورة",start_date=timezone.localdate(),end_date=timezone.localdate(),hours=10,trainer="اسم المدرب",track="")
        enrollment=SimpleNamespace(full_name="اسم المستفيد",program=program,certificate_code="PREVIEW")
        return render(request,"programs/certificate.html",{"company":request.company,"e":enrollment,"ps":ps,"verify_url":"نسخة معاينة"})
    if kind=="reports":
        return render(request,"settings/studio_report_preview.html",{"ps":ps,"company":request.company,"studio":layout(ps,kind)})
    item=SimpleNamespace(number="PREVIEW-0001",subject="معاينة تصميم خطاب رسمي",direction="outgoing",date=timezone.localdate(),party="الجهة المستلمة",salutation="سعادة /",body="هذه نسخة معاينة لتنسيق الخطابات.\n\nتأخذ الهوية اللون والشعار من إعدادات الجهة، وتتغير مقاسات الصفحة والترويسة والتوقيع حسب التصميم المختار.",issuer_name="اسم الموظف المصدر",issuer_title="المسمى الوظيفي",signature="",attachment_description="ملف واحد",is_confidential=True,get_absolute_url="/settings/studio/")
    return render(request,"office/letter.html",{"item":item,"company":request.company,"ps":ps,"barcode":svg(item.number,height=42),"studio":layout(ps)})
