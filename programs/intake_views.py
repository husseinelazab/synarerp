import hashlib
import json
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Q, F, DurationField, ExpressionWrapper
from django.db.models.functions import TruncDate
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache

from core.permissions import require_submit
from programs.intake import review_application, schedule_health_application
from programs.intake_forms import SpecialtyRegistrationForm, SpecialtyReviewForm, HealthSchedulingForm
from programs.models import (SpecialtyApplication, QuranCircle, QuranStudent, QuranProgress,
    NewMuslim, DawahActivity, UmrahTrip, UmrahParticipant, HealthPatient, HealthAppointment)
from website.ratelimit import LIMIT_MESSAGE, too_many
from website.site import site_view


@site_view
@require_http_methods(["GET", "POST"])
def public_registration(request):
    site = request.site
    if not site.specialty_registration_available:
        raise Http404
    form = SpecialtyRegistrationForm(request.POST if request.method == "POST" else None, company=site.company)
    done = False
    limited = request.method == "POST" and too_many(request, "specialty-registration", 15)
    if limited:
        form.add_error(None, LIMIT_MESSAGE)
    elif request.method == "POST" and form.is_valid():
        if not form.cleaned_data.get("website"):
            values = {name: form.cleaned_data.get(name) for name in form._meta.fields if name in form.fields}
            canonical = {key: getattr(value, "pk", value) for key, value in values.items()}
            fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
            from website.public_views import _notify, erp_url
            with transaction.atomic():
                row, created = SpecialtyApplication.objects.get_or_create(company=site.company,
                    submission_key=hashlib.sha256(f"{site.company.association_specialty}:{fingerprint}".encode()).hexdigest(),
                    defaults=dict(values, specialty=site.company.association_specialty))
                if created:
                    url = erp_url("specialty_review", row.pk)
                    transaction.on_commit(lambda: _notify(site.company, "طلب تسجيل تخصصي جديد بانتظار المراجعة", url))
        done = True
    response = render(request, "website/specialty_register.html", {"form": form, "done": done})
    response["Cache-Control"] = "private, no-store"
    return response


def _scope(request, write=False):
    company = request.company
    if not company.is_nonprofit or company.association_specialty not in {"quran", "dawah", "health"}:
        raise Http404
    membership = getattr(request, "membership", None)
    if membership and (not membership.allows_module(company.association_specialty) or
                       membership.doctype_access("specialty-application") == "none"):
        raise PermissionDenied
    if write:
        require_submit(request)
        if membership and membership.doctype_access("specialty-application") != "write":
            raise PermissionDenied
    return SpecialtyApplication.objects.filter(company=company, specialty=company.association_specialty)


def _can_read(request, slug):
    membership = getattr(request, "membership", None)
    return membership is None or membership.doctype_access(slug) != "none"


def _check_targets(request, row, data):
    if data["decision"] != "accept":
        return
    targets = {"quran": ["quran-student"], "dawah": ["new-muslim"], "health": ["health-patient"]}[row.specialty]
    if row.trip_id:
        targets.append("umrah-participant")
    if data.get("appointment_at"):
        targets.append("health-appointment")
    membership = getattr(request, "membership", None)
    if membership and any(membership.doctype_access(slug) != "write" for slug in targets):
        raise PermissionDenied


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def review(request, pk):
    row = get_object_or_404(_scope(request), pk=pk)
    # Restrict before style_form() can register a remote-select query token.
    slug = {"quran": "quran-student", "dawah": "new-muslim", "health": "health-patient"}[row.specialty]
    form = SpecialtyReviewForm(request.POST if request.method == "POST" else None, application=row,
                               allow_existing_profile=_can_read(request, slug))
    scheduling = row.specialty == "health" and row.status == "accepted" and not row.appointment_id
    is_schedule = request.method == "POST" and request.POST.get("action") == "schedule"
    schedule_form = HealthSchedulingForm(request.POST if is_schedule else None) if scheduling else None
    if request.method == "POST":
        try:
            _scope(request, write=True)
            if is_schedule:
                membership = getattr(request, "membership", None)
                if not _can_read(request, "health-patient") or (membership and membership.doctype_access("health-appointment") != "write"):
                    raise PermissionDenied
                if schedule_form is None:
                    raise ValidationError("لا يمكن إضافة موعد جديد لهذا الطلب.")
                if schedule_form.is_valid():
                    schedule_health_application(row, schedule_form.cleaned_data)
                    messages.success(request, "تم حجز الموعد وربطه بالطلب.")
                    return redirect("specialty_review", pk=pk)
            elif form.is_valid():
                _check_targets(request, row, form.cleaned_data)
                review_application(row, request.user, form.cleaned_data)
                messages.success(request, "تم توثيق قرار المراجعة وتحديث السجلات المعتمدة.")
                return redirect("specialty_review", pk=pk)
        except ValidationError as exc:
            (schedule_form if is_schedule and schedule_form is not None else form).add_error(None, " · ".join(exc.messages))
    membership = getattr(request, "membership", None)
    can_review = membership is None or (membership.may_submit and membership.doctype_access("specialty-application") == "write")
    linked_records = []
    for name, target_slug in [("student", "quran-student"), ("new_muslim", "new-muslim"), ("participant", "umrah-participant"), ("patient", "health-patient"), ("appointment", "health-appointment")]:
        if getattr(row, f"{name}_id") and _can_read(request, target_slug):
            record = getattr(row, name)
            linked_records.append({"label": record._meta.verbose_name, "record": record})
    may_schedule = can_review and _can_read(request, "health-patient") and (membership is None or membership.doctype_access("health-appointment") == "write")
    return render(request, "programs/specialty_review.html", {"title": "مراجعة طلب التسجيل", "application": row,
        "form": form, "can_review": can_review, "schedule_form": schedule_form if may_schedule else None, "linked_records": linked_records,
        "details": [(field.verbose_name, getattr(row, f"get_{field.name}_display", lambda: getattr(row, field.name))())
                    for field in row._meta.fields if field.name in {"reference", "specialty", "full_name", "phone", "email", "circle", "trip", "nationality", "language", "birth_date", "service_needed", "preferred_date", "notes", "created_at", "consent_at"} and getattr(row, field.name)]})


@login_required
@never_cache
@require_http_methods(["GET"])
def dashboard(request):
    applications = _scope(request)
    today = timezone.localdate()
    since = today - timedelta(days=30)
    pending = applications.filter(status="pending")
    counts = applications.aggregate(total=Count("pk"), pending=Count("pk", filter=Q(status="pending")),
        accepted=Count("pk", filter=Q(status="accepted")), rejected=Count("pk", filter=Q(status="rejected")))
    average = applications.exclude(reviewed_at=None).aggregate(duration=Avg(ExpressionWrapper(
        F("reviewed_at") - F("created_at"), output_field=DurationField())))["duration"]
    average_hours = round(average.total_seconds() / 3600, 1) if average is not None else None
    overdue = pending.filter(created_at__lt=timezone.now() - timedelta(hours=48)).count()
    status = request.GET.get("status", "pending")
    if status in {"pending", "accepted", "rejected"}:
        applications = applications.filter(status=status)
    else:
        status = "all"
    query = request.GET.get("q", "").strip()[:150]
    if query:
        applications = applications.filter(Q(full_name__icontains=query) | Q(phone__icontains=query))
    date_form = DateRangeForm(request.GET)
    if date_form.is_valid():
        if date_form.cleaned_data.get("from_date"):
            applications = applications.filter(created_at__date__gte=date_form.cleaned_data["from_date"])
        if date_form.cleaned_data.get("to_date"):
            applications = applications.filter(created_at__date__lte=date_form.cleaned_data["to_date"])
    cards, queues, tasks = [], [], []
    def card(label, model, slug, filters=None):
        if _can_read(request, slug):
            cards.append({"label": label, "value": model.objects.filter(company=request.company, **(filters or {})).count(),
                          "url": f"/app/{slug}/"})
    specialty = request.company.association_specialty
    if specialty == "quran":
        card("الحلقات النشطة", QuranCircle, "quran-circle", {"disabled": False})
        card("الطلاب النشطون", QuranStudent, "quran-student", {"disabled": False})
        card("متابعات الحفظ اليوم", QuranProgress, "quran-progress", {"date": today})
        card("حالات الغياب خلال 30 يوماً", QuranProgress, "quran-progress", {"date__gte": since, "date__lte": today, "attended": False})
        if _can_read(request, "quran-circle") and _can_read(request, "quran-student"):
            circles = QuranCircle.objects.filter(company=request.company, disabled=False).annotate(
                occupied=Count("quranstudent", filter=Q(quranstudent__disabled=False))).order_by("name")
            queues.append({"title": "إشغال الحلقات", "rows": [(c.name, f"{c.occupied} / {c.capacity}", c.get_absolute_url()) for c in circles[:30]]})
        if _can_read(request, "quran-student") and _can_read(request, "quran-progress"):
            untracked = QuranStudent.objects.filter(company=request.company, disabled=False).exclude(
                pk__in=QuranProgress.objects.filter(company=request.company, date=today).values("student_id"))
            tasks.append({"label": "طلاب دون رصد متابعة اليوم (لا يعني الغياب)", "count": untracked.count()})
            queues.append({"title": "طلاب يحتاجون تسجيل متابعة اليوم", "rows": [(s.full_name, "لم تُرصد متابعة اليوم", s.get_absolute_url()) for s in untracked.order_by("full_name")[:30]]})
    elif specialty == "dawah":
        card("المسلمون الجدد", NewMuslim, "new-muslim")
        card("البرامج الدعوية القادمة", DawahActivity, "dawah-activity", {"date__gte": today})
        card("رحلات العمرة المفتوحة", UmrahTrip, "umrah-trip", {"status": "open", "departure_date__gte": today})
        card("مشاركات العمرة المؤكدة", UmrahParticipant, "umrah-participant", {"status": "confirmed", "trip__departure_date__gte": today})
        if _can_read(request, "umrah-trip") and _can_read(request, "umrah-participant"):
            trips = UmrahTrip.objects.filter(company=request.company, departure_date__gte=today).exclude(status__in=["cancelled", "completed"]).order_by("departure_date")
            queues.append({"title": "الرحلات القادمة والمقاعد المتبقية", "rows": [(t.title, f"{t.departure_date} · {t.seats_remaining} مقعد", t.get_absolute_url()) for t in trips[:30]]})
        if _can_read(request, "new-muslim"):
            unassigned = NewMuslim.objects.filter(company=request.company, mentor="")
            tasks.append({"label": "مستفيدون دون داعية / متابع", "count": unassigned.count()})
            queues.append({"title": "ملفات تحتاج تعيين متابع", "rows": [(m.full_name, m.language, m.get_absolute_url()) for m in unassigned.order_by("full_name")[:30]]})
    else:
        card("ملفات المرضى", HealthPatient, "health-patient")
        card("مواعيد اليوم", HealthAppointment, "health-appointment", {"scheduled_at__date": today, "status": "scheduled"})
        card("حالات الحضور خلال 30 يوماً", HealthAppointment, "health-appointment", {"scheduled_at__date__gte": since, "scheduled_at__date__lte": today, "status": "attended"})
        card("مواعيد فائتة دون تحديث", HealthAppointment, "health-appointment", {"scheduled_at__lt": timezone.now(), "status": "scheduled"})
        if _can_read(request, "health-appointment") and _can_read(request, "health-patient"):
            appointments = HealthAppointment.objects.filter(company=request.company, status="scheduled", scheduled_at__gte=timezone.now()).select_related("patient").order_by("scheduled_at")
            queues.append({"title": "المواعيد القادمة", "rows": [(a.patient.full_name, a.scheduled_at, a.get_absolute_url()) for a in appointments[:30]]})
        if _can_read(request, "health-patient") and _can_read(request, "health-appointment"):
            waiting = _scope(request).filter(status="accepted", appointment=None).select_related("patient")
            tasks.append({"label": "طلبات صحية مقبولة دون موعد مرتبط بالطلب", "count": waiting.count()})
            queues.append({"title": "طلبات صحية مقبولة تحتاج تنسيق الخدمة", "rows": [(a.full_name, a.service_needed, reverse("specialty_review", args=[a.pk])) for a in waiting[:30] if a.patient_id]})
    trend = list(_scope(request).filter(created_at__date__gte=today-timedelta(days=6)).annotate(day=TruncDate("created_at")).values("day").annotate(total=Count("pk")).order_by("day"))
    daily = {r["day"]: r["total"] for r in trend}
    trend = [{"day": today-timedelta(days=i), "total": daily.get(today-timedelta(days=i), 0)} for i in range(6, -1, -1)]
    maximum = max([r["total"] for r in trend] + [1])
    for point in trend:
        point["percent"] = round(point["total"] * 100 / maximum)
    from website.models import Website
    site = Website.objects.filter(company=request.company).first()
    return render(request, "programs/specialty_dashboard.html", {"title": "لوحة متابعة التسجيل والخدمات التخصصية",
        "counts": counts, "overdue": overdue, "cards": cards, "queues": queues, "tasks": tasks, "average_hours": average_hours, "trend": trend,
        "page_obj": Paginator(applications, 25).get_page(request.GET.get("page")),
        "q": query, "status": status, "date_form": date_form, "site": site,
        "oldest": pending.order_by("created_at").first(),
        "recent_decisions": _scope(request).exclude(status="pending").select_related("reviewed_by").order_by("-reviewed_at")[:8]})


class DateRangeForm(forms.Form):
    from_date = forms.DateField(label="من تاريخ", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(label="إلى تاريخ", required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        data = super().clean()
        if data.get("from_date") and data.get("to_date") and data["from_date"] > data["to_date"]:
            raise forms.ValidationError("بداية الفترة لا تتجاوز نهايتها.")
        return data
