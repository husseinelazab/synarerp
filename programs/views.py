import datetime
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from beneficiaries.models import Beneficiary, Gender, validate_national_id
from core.forms import style_form
from core.tenancy import tenant_context
from core.utils import flt
from programs.models import (ADMITTED_STATUSES, EnrollmentStatus, ProgramEnrollment, ProgramStatus, SessionAttendance,
                             TrainingProgram)
from website.ratelimit import LIMIT_MESSAGE, too_many
from website.site import render_public


@login_required
def programs_dashboard(request):
    year = timezone.localdate().year
    admitted = ProgramEnrollment.objects.filter(status__in=ADMITTED_STATUSES, program__start_date__year=year)
    passed = admitted.filter(status=EnrollmentStatus.PASSED).select_related("program")
    recent = list(TrainingProgram.objects.filter(status=ProgramStatus.COMPLETED).order_by("-end_date")[:8])
    for program in recent:
        program.metrics = program.impact()
    return render(request, "programs/dashboard.html", {
        "title": "لوحة البرامج والتطوير", "year": year,
        "active": TrainingProgram.objects.filter(status__in=[ProgramStatus.OPEN, ProgramStatus.RUNNING]).count(),
        "trainees": admitted.count(),
        "hours": flt(sum((e.program.hours for e in passed), Decimal(0)), 1),
        "satisfaction": flt(admitted.aggregate(a=Avg("satisfaction"))["a"]) if admitted.exclude(
            satisfaction=None).exists() else None,
        "programs": TrainingProgram.objects.filter(status__in=[ProgramStatus.OPEN, ProgramStatus.RUNNING])
        .annotate(n=Count("enrollments", filter=Q(enrollments__status__in=ADMITTED_STATUSES)),
                  waiting=Count("enrollments", filter=Q(enrollments__status=EnrollmentStatus.REGISTERED)))
        .order_by("start_date"),
        "pending": ProgramEnrollment.objects.filter(status=EnrollmentStatus.REGISTERED,
                                                    program__status__in=[ProgramStatus.OPEN, ProgramStatus.RUNNING,
                                                                         ProgramStatus.DRAFT])
        .select_related("program")[:50],
        "recent": recent,
    })


def _decimal(value, low, high):
    """قيمة رقمية من الإدخال ضمن المدى، أو None إن كانت فارغة أو غير صالحة."""
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None
    return number if low <= number <= high else None


@login_required
def attendance_sheet(request, pk):
    """كشف الحضور: المتدربون المقبولون × الجلسات، مع درجات الاختبار القبلي والبعدي والرضا."""
    program = get_object_or_404(TrainingProgram, pk=pk)
    sessions = list(program.sessions.all())
    enrollments = list(program.admitted.order_by("full_name"))
    if request.method == "POST":
        if program.status == ProgramStatus.CANCELLED:
            messages.error(request, "البرنامج ملغى.")
            return redirect("program_attendance", pk=pk)
        with transaction.atomic():
            for enrollment in enrollments:
                for session in sessions:
                    SessionAttendance.objects.update_or_create(
                        enrollment=enrollment, session=session,
                        defaults={"present": f"att_{enrollment.pk}_{session.pk}" in request.POST})
                enrollment.pre_score = _decimal(request.POST.get(f"pre_{enrollment.pk}", ""), 0, 100)
                enrollment.post_score = _decimal(request.POST.get(f"post_{enrollment.pk}", ""), 0, 100)
                satisfaction = _decimal(request.POST.get(f"sat_{enrollment.pk}", ""), 1, 5)
                enrollment.satisfaction = int(satisfaction) if satisfaction is not None else None
                enrollment.save(update_fields=["pre_score", "post_score", "satisfaction"])
        messages.success(request, "تم حفظ الحضور والدرجات.")
        return redirect("program_attendance", pk=pk)
    present = set(SessionAttendance.objects.filter(enrollment__program=program, present=True)
                  .values_list("enrollment_id", "session_id"))
    rows = [{"e": e, "cells": [(s, (e.pk, s.pk) in present) for s in sessions]} for e in enrollments]
    return render(request, "programs/attendance.html", {
        "title": f"كشف الحضور والتقييم — {program.title}", "program": program, "sessions": sessions, "rows": rows})


@login_required
def impact_report(request):
    """تقرير أثر البرامج المنفذة في فترة: الملتحقون والمجتازون والتحسن بين القبلي والبعدي والرضا."""
    today = timezone.localdate()
    try:
        from_date = datetime.date.fromisoformat(request.GET.get("from_date", ""))
    except ValueError:
        from_date = today.replace(month=1, day=1)
    try:
        to_date = datetime.date.fromisoformat(request.GET.get("to_date", ""))
    except ValueError:
        to_date = today.replace(month=12, day=31)
    programs = list(TrainingProgram.objects.filter(start_date__lte=to_date, end_date__gte=from_date)
                    .exclude(status=ProgramStatus.CANCELLED).order_by("start_date"))
    totals = {"enrolled": 0, "completed": 0, "trained_hours": Decimal(0)}
    for program in programs:
        program.metrics = program.impact()
        for key in totals:
            totals[key] += program.metrics[key]
    totals["completion_rate"] = (flt(Decimal(totals["completed"]) * 100 / totals["enrolled"], 1)
                                 if totals["enrolled"] else None)
    return render(request, "programs/impact.html", {
        "title": "تقرير أثر البرامج التدريبية", "programs": programs, "totals": totals,
        "from_date": from_date, "to_date": to_date, "company": request.company})


@login_required
def certificate(request, pk):
    enrollment = get_object_or_404(ProgramEnrollment.objects.select_related("program"), pk=pk,
                                   status=EnrollmentStatus.PASSED)
    return render(request, "programs/certificate.html", {
        "e": enrollment, "company": request.company,
        "verify_url": request.build_absolute_uri(reverse("program_verify", args=[enrollment.certificate_code]))})


class RegistrationForm(forms.Form):
    full_name = forms.CharField(label="الاسم الكامل", max_length=150)
    national_id = forms.CharField(label="رقم الهوية / الإقامة", max_length=10, validators=[validate_national_id])
    phone = forms.CharField(label="الجوال", max_length=20)
    email = forms.EmailField(label="البريد الإلكتروني", required=False)
    gender = forms.ChoiceField(label="الجنس", choices=Gender.choices)
    website = forms.CharField(required=False, widget=forms.HiddenInput)  # مصيدة للبرامج الآلية

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


def public_register(request, token):
    """صفحة عامة بلا تسجيل دخول: يسجل المتدرب نفسه في البرنامج برابطه المنشور، ويظهر طلبه للقبول."""
    program = get_object_or_404(TrainingProgram.all_objects.select_related("company"), public_token=token)
    company = program.company
    subscription = getattr(company, "subscription", None)
    closed = (program.status != ProgramStatus.OPEN or program.end_date < timezone.localdate()
              or not company.is_nonprofit or subscription is None or not subscription.is_valid)
    with tenant_context(company):
        full = program.is_full
    form = RegistrationForm(request.POST or None)
    done = False
    # حد للتسجيل من الجهاز نفسه: الصفحة عامة بلا دخول
    if request.method == "POST" and not closed and too_many(request, "public_register", 10) and form.is_valid():
        form.add_error(None, LIMIT_MESSAGE)
    if request.method == "POST" and not closed and not full and form.is_valid():
        data = form.cleaned_data
        if data["website"]:
            done = True  # بوت: لا نحفظ شيئاً
        else:
            with tenant_context(company):
                try:
                    with transaction.atomic():
                        enrollment = ProgramEnrollment.all_objects.create(
                            company=company, program=program, full_name=data["full_name"],
                            national_id=data["national_id"], phone=data["phone"], email=data["email"],
                            gender=data["gender"],
                            beneficiary=Beneficiary.all_objects.filter(company=company,
                                                                       national_id=data["national_id"]).first())
                except IntegrityError:
                    form.add_error(None, "أنت مسجل في هذا البرنامج مسبقاً، وسنتواصل معك.")
                else:
                    done = True
                    try:
                        from core.notify import notify_managers

                        notify_managers(company, f"طلب تسجيل في «{program.title}»: {enrollment.full_name}",
                                        reverse("doc_detail", args=["program-enrollment", enrollment.pk]))
                    except Exception:  # التنبيه لا يعطل التسجيل
                        pass
    return render_public(request, "programs/public_register.html", "programs/_public_register_body.html",
                         program.title, {"program": program, "form": form, "closed": closed, "full": full,
                                         "done": done}, parent="البرامج التدريبية")


def public_verify(request, code):
    enrollment = ProgramEnrollment.all_objects.select_related("program", "company").filter(
        certificate_code=code.upper(), status=EnrollmentStatus.PASSED).first()
    return render_public(request, "programs/public_verify.html", "programs/_public_verify_body.html",
                         "التحقق من شهادة تدريب", {"e": enrollment, "code": code}, parent="الخدمات الإلكترونية")


@login_required
def umrah_manifest(request, pk):
    from django.core.exceptions import PermissionDenied
    from core.models import PrintSettings
    from programs.models import UmrahTrip
    if request.membership and any(request.membership.doctype_access(slug) == "none"
                                  for slug in ("umrah-trip", "umrah-participant", "new-muslim")):
        raise PermissionDenied
    trip = get_object_or_404(UmrahTrip.objects.filter(company=request.company), pk=pk)
    return render(request, "programs/umrah_manifest.html", {
        "trip": trip, "company": request.company, "ps": PrintSettings.for_company(request.company),
        "participants": trip.participants.exclude(status="cancelled").select_related("participant").order_by("participant__full_name"),
    })
