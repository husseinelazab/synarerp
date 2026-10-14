from campaigns.forms import CampaignForm
from campaigns.models import Campaign, OptOut, Segment
from core.registry import DocType, register

register(DocType(
    slug="campaign-segment", model=Segment, module="resources",
    list_fields=["name", "rule", "months", "min_amount"],
    search_fields=["name", "description"], detail_template="campaigns/_segment_detail.html",
))
register(DocType(
    slug="campaign", model=Campaign, module="resources", form_class=CampaignForm,
    list_fields=["title", "channel", "segment", "scheduled_at", "sent_at", "status"],
    search_fields=["title", "message", "segment__name"], detail_template="campaigns/_campaign_detail.html",
    detail_exclude=["message"],
))
register(DocType(
    slug="campaign-optout", model=OptOut, module="resources",
    list_fields=["phone", "email", "reason", "source", "created_at"],
    search_fields=["phone", "email", "reason"],
))
