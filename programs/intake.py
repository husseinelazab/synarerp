"""Atomic staff decisions: pending public submissions never reserve operational capacity."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Company
from programs.models import (SpecialtyApplication, QuranStudent, NewMuslim, UmrahParticipant,
                             HealthPatient, HealthAppointment)


@transaction.atomic
def review_application(application, user, data):
    # Same lock order as SpecialtyRecord.save(), including seats shared with manual entry.
    company = Company.objects.select_for_update().get(pk=application.company_id)
    row = SpecialtyApplication.objects.select_for_update().get(pk=application.pk, company=company)
    if not company.is_nonprofit or row.specialty != company.association_specialty:
        raise ValidationError("لا يتطابق الطلب مع تخصص الجمعية الحالي.")
    if row.status != "pending":
        raise ValidationError("سبق اتخاذ قرار لهذا الطلب؛ لم يُنشأ أي سجل إضافي.")
    if data["decision"] not in {"accept", "reject"}:
        raise ValidationError("قرار غير صالح.")
    if data["decision"] == "reject" and not data.get("review_note", "").strip():
        raise ValidationError("سبب الرفض مطلوب.")
    if data["decision"] == "accept":
        profile = data.get("existing_profile")
        profile_model = {"quran": QuranStudent, "dawah": NewMuslim, "health": HealthPatient}[row.specialty]
        if profile is not None:
            if not isinstance(profile, profile_model) or profile.company_id != company.pk:
                raise ValidationError("الملف المختار لا يخص الجمعية أو التخصص.")
            profile = profile_model.objects.get(pk=profile.pk, company=company)
        if row.specialty == "quran":
            circle = data.get("circle")
            if profile and (profile.disabled or (circle and profile.circle_id != circle.pk)):
                raise ValidationError("الطالب مسجل في حلقة مختلفة أو موقوف؛ راجع ملفه أولاً.")
            row.student = profile or QuranStudent.objects.create(company=company, full_name=row.full_name,
                                                                  guardian_phone=row.phone, circle=circle)
        elif row.specialty == "dawah":
            row.new_muslim = profile or NewMuslim.objects.create(company=company, full_name=row.full_name,
                phone=row.phone, nationality=row.nationality, language=row.language)
            if row.trip_id:
                trip = row.trip.__class__.objects.get(pk=row.trip_id, company=company)
                if trip.status != "open" or trip.departure_date < timezone.localdate():
                    raise ValidationError("الرحلة مغلقة أو انقضى تاريخها؛ لم يتم قبول الطلب.")
                if UmrahParticipant.objects.filter(trip=trip, participant=row.new_muslim).exists():
                    raise ValidationError("هذا المستفيد مسجل بالفعل في الرحلة. راجع مشاركته الحالية.")
                row.participant = UmrahParticipant.objects.create(company=company, trip=trip, participant=row.new_muslim)
        else:
            row.patient = profile or HealthPatient.objects.create(company=company, file_number=f"WEB-{row.pk}",
                full_name=row.full_name, phone=row.phone, birth_date=row.birth_date, service_needed=row.service_needed)
            if data.get("appointment_at"):
                row.appointment = HealthAppointment.objects.create(company=company, patient=row.patient,
                    scheduled_at=data["appointment_at"], clinician=data["clinician"], clinic=data["clinic"])
        row.status = "accepted"
    else:
        row.status = "rejected"
    row.review_note = data.get("review_note", "")
    row.reviewed_at = timezone.now()
    row.reviewed_by = user
    row.save()
    return row


@transaction.atomic
def schedule_health_application(application, data):
    company = Company.objects.select_for_update().get(pk=application.company_id)
    row = SpecialtyApplication.objects.select_for_update().get(pk=application.pk, company=company)
    if (not company.is_nonprofit or company.association_specialty != "health" or row.specialty != "health"
            or row.status != "accepted" or not row.patient_id):
        raise ValidationError("جدولة الموعد متاحة للطلبات الصحية المقبولة فقط.")
    if row.appointment_id:
        raise ValidationError("سبق ربط موعد بهذا الطلب؛ راجع الموعد الحالي.")
    row.appointment = HealthAppointment.objects.create(company=company, patient=row.patient, **data)
    row.save(update_fields=["appointment"])
    return row
