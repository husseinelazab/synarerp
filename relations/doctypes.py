from core.registry import ChildTable, DocType, register
from relations.models import MediaCoverage, Partnership, PRContact, PREvent, Survey, SurveyQuestion, Visit

register(DocType(
    slug="pr-contact", model=PRContact, module="relations",
    list_fields=["name", "organization", "job_title", "category", "phone", "city", "is_active"],
    search_fields=["name", "organization", "job_title", "phone", "email", "city"],
))
register(DocType(
    slug="partnership", model=Partnership, module="relations",
    list_fields=["partner", "partnership_type", "agreement_number", "start_date", "end_date", "value", "status"],
    search_fields=["partner", "agreement_number", "objectives", "contact__name"],
))
register(DocType(
    slug="pr-event", model=PREvent, module="relations",
    list_fields=["title", "event_type", "start_date", "location", "expected_attendees", "actual_attendees", "status"],
    search_fields=["title", "location", "outcomes"],
))
register(DocType(
    slug="visit", model=Visit, module="relations",
    list_fields=["visit_date", "direction", "entity", "purpose", "follow_up_date", "status"],
    search_fields=["entity", "purpose", "attendees", "contact__name"],
))
register(DocType(
    slug="media-coverage", model=MediaCoverage, module="relations",
    list_fields=["published_on", "title", "channel", "reach", "event"],
    search_fields=["title", "url", "event__title"],
))
register(DocType(
    slug="survey", model=Survey, module="relations",
    list_fields=["title", "audience", "is_open", "end_date"],
    search_fields=["title", "audience", "description"], detail_template="relations/_survey_detail.html",
    child=ChildTable(SurveyQuestion, ["text", "question_type", "choices", "required"], label="الأسئلة"),
))
