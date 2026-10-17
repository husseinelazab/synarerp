"""Idempotent, additive tenant presets; existing data and member permissions stay authoritative."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.shortcuts import redirect,render
from django.urls import reverse
from core.forms import style_form
from core.models import Company,ProcessTemplate,ProcessStage,PrintSettings
from core.workspace import can_module,can_read

PRESETS={"general":"جمعية عامة","quran":"جمعية تحفيظ","dawah":"جمعية دعوة ورعاية المسلمين الجدد","health":"جمعية صحية","retail":"شركة أو مؤسسة تجارية","services":"شركة أو مؤسسة خدمية"}


def available(request):
    return [k for k in PRESETS if (k in ("retail","services"))!=request.company.is_nonprofit]


def has_specialty_data(company):
    from programs.models import QuranCircle,NewMuslim,HealthPatient,UmrahTrip
    return any(model.objects.filter(company=company).exists() for model in (QuranCircle,NewMuslim,HealthPatient,UmrahTrip))


class SetupForm(forms.Form):
    preset=forms.ChoiceField(label="نموذج الجهة")
    confirm=forms.BooleanField(label="راجعت التجهيزات وأريد إضافة العناصر غير الموجودة فقط",required=True)
    def __init__(self,*args,request,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["preset"].choices=[(k,PRESETS[k]) for k in available(request)]
        self.fields["preset"].initial=request.company.association_specialty if request.company.is_nonprofit else "retail"
        if request.company.is_nonprofit and has_specialty_data(request.company):
            self.fields["preset"].disabled=True
            self.fields["preset"].help_text="تخصص الجهة ثابت هنا بعد وجود سجلات تشغيلية؛ تبقى البيانات الحالية محفوظة."
        style_form(self)


def specification(request,preset):
    if request.company.is_nonprofit:
        module="beneficiaries" if preset=="general" else preset
        processes={"general":"مراجعة طلب خدمة مستفيد","quran":"مراجعة قبول طالب حلقة","dawah":"تجهيز واعتماد رحلة عمرة","health":"مراجعة طلب تنسيق خدمة صحية"}
        modules=["accounts","beneficiaries","office","documents","programs",module]
        return {"units":[("الجمعية العمومية","assembly",None),("مجلس الإدارة","board","الجمعية العمومية"),("الإدارة التنفيذية","executive","مجلس الإدارة"),("لجنة البرامج والخدمات","committee","مجلس الإدارة"),("إدارة البرامج والخدمات","department","الإدارة التنفيذية")],"roles":[("منسق الخدمات",[module,"beneficiaries","office"]),("مسؤول التوثيق",["documents","office"])],"process":processes.get(preset,processes["general"]),"module":module,"modules":modules,"imports":["beneficiary","donor"]+({"quran":["quran-circle","quran-student"],"dawah":["new-muslim"],"health":["health-patient"]}.get(preset,[]))}
    return {"units":[],"roles":[],"process":"مراجعة طلب شراء" if preset=="retail" else "مراجعة طلب تنفيذ خدمة","module":"buying" if preset=="retail" else "projects","modules":["accounts","buying","stock","selling"] if preset=="retail" else ["accounts","projects","selling"],"imports":["item","customer","supplier"]}


@transaction.atomic
def apply(request,preset):
    company=Company.objects.select_for_update().get(pk=request.company.pk)
    if company.is_nonprofit and has_specialty_data(company) and preset!=company.association_specialty:
        from django.core.exceptions import ValidationError
        raise ValidationError("لا يُغيَّر التخصص بعد إنشاء سجلاته التشغيلية.")
    spec=specification(request,preset)
    if company.is_nonprofit:
        if company.association_specialty!=preset:
            company.association_specialty=preset;company.save(update_fields=["association_specialty"])
            request.company.association_specialty=preset
            request.membership.company.association_specialty=preset
        from hr.models import OrganizationUnit,AssociationRole
        for name,kind,parent_name in spec["units"]:
            parent=OrganizationUnit.objects.filter(company=company,name=parent_name).first() if parent_name else None
            OrganizationUnit.objects.get_or_create(company=company,name=name,defaults={"kind":kind,"parent":parent})
        for name,keys in spec["roles"]:
            AssociationRole.objects.get_or_create(company=company,name=name,defaults={"modules":keys,"can_submit":False,"can_delete":False,"can_cancel":False})
    if can_module(request,spec["module"]):
        template,created=ProcessTemplate.objects.get_or_create(company=company,name=spec["process"],defaults={"module":spec["module"],"enabled":False,"description":"قالب بدء مقترح. راجع المسؤولين والمدة ثم فعّله من منشئ الإجراءات."})
        if created:
            ProcessStage.objects.create(template=template,position=1,name="مراجعة البيانات والمتطلبات",role="manager",days=3)
            ProcessStage.objects.create(template=template,position=2,name="اعتماد التنفيذ",role="owner",days=2)
    ps,_=PrintSettings.objects.get_or_create(company=company)
    if not ps.document_layout:
        ps.document_layout={"margin":"14","logo_width":"58","card_width":"68","font_size":"15","line_height":"2","identity_side":"right","signature_side":"left"};ps.save(update_fields=["document_layout"])


@login_required
def wizard(request):
    if not request.membership.can_manage:raise PermissionDenied
    form=SetupForm(request.POST if request.method=="POST" else None,request=request)
    selected=request.POST.get("preset") if request.method=="POST" else request.GET.get("preset")
    if selected not in available(request):selected=request.company.association_specialty if request.company.is_nonprofit else "retail"
    if form.fields["preset"].disabled:selected=request.company.association_specialty
    form.fields["preset"].initial=selected
    spec=specification(request,selected)
    if request.method=="POST" and form.is_valid():
        try:
            apply(request,form.cleaned_data["preset"])
            messages.success(request,"اكتمل تجهيز العناصر غير الموجودة. راجع القوالب والصلاحيات قبل تفعيلها وربطها بالمستخدمين.")
            return redirect("setup_wizard")
        except ValidationError as exc:
            form.add_error(None,exc)
    from core.registry import REGISTRY
    imports=[{"slug":slug,"label":REGISTRY[slug].label_plural,"url":reverse("doc_import",args=[slug])} for slug in spec["imports"] if can_read(request,slug)]
    return render(request,"settings/setup_wizard.html",{"form":form,"spec":spec,"imports":imports,"presets":[(k,PRESETS[k]) for k in available(request)]})
