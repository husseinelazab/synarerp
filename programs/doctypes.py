from core.registry import ChildTable, DocType, register
from programs.models import ProgramEnrollment, ProgramSession, TrainingProgram

register(DocType(
    slug="training-program", model=TrainingProgram, module="programs",
    list_fields=["title", "program_type", "track", "trainer", "start_date", "end_date", "capacity", "status"],
    search_fields=["title", "track", "trainer", "location", "target_audience"],
    child=ChildTable(ProgramSession, ["date", "topic"], label="جلسات البرنامج", min_rows=0,
                     help="جلسات البرنامج تُرصد عليها نسبة الحضور (بلا جلسات يُعد الحضور مكتملاً)"),
    detail_template="programs/_program_detail.html",
))
register(DocType(
    slug="program-enrollment", model=ProgramEnrollment, module="programs",
    list_fields=["full_name", "national_id", "phone", "program", "status", "post_score", "satisfaction"],
    search_fields=["full_name", "national_id", "phone", "program__title"],
    detail_template="programs/_enrollment_detail.html",
))


from programs.models import (QuranCircle, QuranStudent, QuranProgress, NewMuslim, DawahActivity,
                             UmrahTrip, UmrahParticipant, HealthPatient, HealthAppointment)

for slug, model, module, columns, search in [
    ("quran-circle", QuranCircle, "quran", ["name", "teacher", "location", "schedule", "capacity"], ["name", "teacher"]),
    ("quran-student", QuranStudent, "quran", ["full_name", "circle", "guardian_phone", "joined_on"], ["full_name", "guardian_phone"]),
    ("quran-progress", QuranProgress, "quran", ["student", "date", "memorization", "revision", "attended", "score"], ["student__full_name"]),
    ("new-muslim", NewMuslim, "dawah", ["full_name", "language", "nationality", "conversion_date", "mentor"], ["full_name", "phone", "language"]),
    ("dawah-activity", DawahActivity, "dawah", ["title", "date", "preacher", "language", "participants"], ["title", "preacher"]),
    ("umrah-trip", UmrahTrip, "dawah", ["title", "departure_date", "return_date", "supervisor", "capacity", "status"], ["title", "supervisor"]),
    ("umrah-participant", UmrahParticipant, "dawah", ["trip", "participant", "status"], ["participant__full_name", "trip__title"]),
    ("health-patient", HealthPatient, "health", ["file_number", "full_name", "phone", "service_needed"], ["file_number", "full_name", "phone"]),
    ("health-appointment", HealthAppointment, "health", ["patient", "scheduled_at", "clinician", "clinic", "status"], ["patient__full_name", "clinician"]),
]:
    register(DocType(slug=slug, model=model, module=module, list_fields=columns, search_fields=search,
                     detail_template="programs/_umrah_trip_detail.html" if slug == "umrah-trip" else ""))

from programs.models import SpecialtyApplication

register(DocType(
    slug="specialty-application", model=SpecialtyApplication, module="programs",
    list_fields=["full_name", "phone", "specialty", "created_at", "status"],
    search_fields=["full_name", "phone"], read_only=True, show_in_menu=False,
    detail_exclude=["submission_key"], detail_template="programs/_application_detail.html",
))

from programs.models import QuranLearningPlan,DawahFollowUp,UmrahChecklist,HealthReferral
register(DocType(slug="quran-learning-plan",model=QuranLearningPlan,module="quran",list_fields=["student","title","start_date","end_date","target_pages","active"],search_fields=["title","student__full_name"],detail_template="programs/_learning_plan.html"))
register(DocType(slug="dawah-follow-up",model=DawahFollowUp,module="dawah",list_fields=["person","title","kind","assigned_to","due_date","completed"],search_fields=["title","person__full_name","outcome"]))
register(DocType(slug="umrah-checklist",model=UmrahChecklist,module="dawah",list_fields=["trip","title","category","assigned_to","due_date","completed"],search_fields=["title","trip__title","notes"]))
register(DocType(slug="health-referral",model=HealthReferral,module="health",list_fields=["patient","provider","service","requested_on","appointment_at","status"],search_fields=["patient__full_name","provider","service"],detail_template="programs/_health_referral.html"))
