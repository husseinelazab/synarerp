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
