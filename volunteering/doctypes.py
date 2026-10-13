from core.registry import DocType, register
from volunteering.models import Volunteer, VolunteerOpportunity, VolunteerParticipation

register(DocType(
    slug="volunteer", model=Volunteer, module="volunteering",
    list_fields=["full_name", "national_id", "phone", "city", "skills", "status"],
    search_fields=["full_name", "national_id", "phone", "skills"], detail_template="volunteering/_volunteer_detail.html",
))
register(DocType(
    slug="volunteer-opportunity", model=VolunteerOpportunity, module="volunteering",
    list_fields=["title", "start_date", "end_date", "required_count", "status"],
    search_fields=["title", "location"], detail_template="volunteering/_opportunity_detail.html",
))
register(DocType(
    slug="volunteer-participation", model=VolunteerParticipation, module="volunteering",
    list_fields=["volunteer", "opportunity", "status", "hours", "rating"],
    search_fields=["volunteer__full_name", "volunteer__phone", "opportunity__title"],
))
