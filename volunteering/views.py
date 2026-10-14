from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from beneficiaries.models import Gender, validate_national_id
from core.forms import style_form
from core.tenancy import tenant_context
from core.utils import flt
from volunteering.models import (OpportunityStatus, ParticipationStatus, Volunteer, VolunteerOpportunity,
                                 VolunteerParticipation)
from website.site import render_public


@login_required
def volunteering_dashboard(request):
    year = timezone.localdate().year
    completed = VolunteerParticipation.objects.filter(status=ParticipationStatus.COMPLETED,
                                                      opportunity__start_date__year=year)
    return render(request, "volunteering/dashboard.html", {
        "title": "لوحة إدارة التطوع", "year": year,
        "volunteers": Volunteer.objects.count(),
        "hours": flt(completed.aggregate(t=Sum("hours"))["t"]), "participations": completed.count(),
        "open": VolunteerOpportunity.objects.filter(status=OpportunityStatus.OPEN),
        "pending": VolunteerParticipation.objects.filter(status=ParticipationStatus.REGISTERED)
        .select_related("volunteer", "opportunity")[:50],
        "top": Volunteer.objects.filter(participations__in=completed).annotate(h=Sum("participations__hours"),
                                                                               n=Count("participations"))
        .order_by("-h")[:10],
    })


@login_required
def certificate(request, pk):
    participation = get_object_or_404(VolunteerParticipation.objects.select_related("volunteer", "opportunity"), pk=pk,
                                      status=ParticipationStatus.COMPLETED)
    return render(request, "volunteering/certificate.html", {
        "p": participation, "company": request.company,
        "verify_url": request.build_absolute_uri(f"/v/verify/{participation.certificate_code}/")})


class RegistrationForm(forms.Form):
    full_name = forms.CharField(label="الاسم الكامل", max_length=150)
    national_id = forms.CharField(label="رقم الهوية / الإقامة", max_length=10, validators=[validate_national_id])
    phone = forms.CharField(label="الجوال", max_length=20)
    email = forms.EmailField(label="البريد الإلكتروني", required=False)
    gender = forms.ChoiceField(label="الجنس", choices=Gender.choices)
    city = forms.CharField(label="المدينة", max_length=60, required=False)
    skills = forms.CharField(label="المهارات والاهتمامات", max_length=255, required=False)
    national_platform_id = forms.CharField(label="رقمك في المنصة الوطنية للعمل التطوعي (إن وجد)", max_length=30,
                                           required=False)
    website = forms.CharField(required=False, widget=forms.HiddenInput)  # مصيدة للبرامج الآلية

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


def public_register(request, token):
    """صفحة عامة بلا تسجيل دخول: يسجل المتطوع نفسه في الفرصة برابطها المنشور."""
    opportunity = get_object_or_404(VolunteerOpportunity.all_objects.select_related("company"), public_token=token)
    subscription = getattr(opportunity.company, "subscription", None)
    closed = (opportunity.status != OpportunityStatus.OPEN or opportunity.end_date < timezone.localdate()
              or not opportunity.company.is_nonprofit or subscription is None or not subscription.is_valid)
    form = RegistrationForm(request.POST or None)
    done = False
    if request.method == "POST" and not closed and form.is_valid():
        data = form.cleaned_data
        if data["website"]:
            done = True  # بوت: لا نحفظ شيئاً
        elif opportunity.gender and data["gender"] != opportunity.gender:
            form.add_error("gender", f"هذه الفرصة مخصصة لـ{Gender(opportunity.gender).label}.")
        else:
            with tenant_context(opportunity.company):
                try:
                    with transaction.atomic():
                        volunteer = Volunteer.all_objects.filter(company=opportunity.company,
                                                                 national_id=data["national_id"]).first()
                        if volunteer is None:
                            volunteer = Volunteer.all_objects.create(
                                company=opportunity.company, full_name=data["full_name"],
                                national_id=data["national_id"], phone=data["phone"], email=data["email"],
                                gender=data["gender"], city=data["city"], skills=data["skills"],
                                national_platform_id=data["national_platform_id"])
                        VolunteerParticipation.all_objects.create(company=opportunity.company,
                                                                  opportunity=opportunity, volunteer=volunteer)
                except IntegrityError:
                    form.add_error(None, "أنت مسجل في هذه الفرصة مسبقاً، وسنتواصل معك.")
                else:
                    done = True
    return render_public(request, "volunteering/public_register.html", "volunteering/_public_register_body.html",
                         opportunity.title, {"opportunity": opportunity, "form": form, "closed": closed,
                                             "done": done}, parent="التطوع")


def public_verify(request, code):
    participation = VolunteerParticipation.all_objects.select_related("volunteer", "opportunity", "company").filter(
        certificate_code=code.upper(), status=ParticipationStatus.COMPLETED).first()
    return render_public(request, "volunteering/public_verify.html", "volunteering/_public_verify_body.html",
                         "التحقق من شهادة تطوع", {"p": participation, "code": code}, parent="الخدمات الإلكترونية")
