from core.registry import DocType, register
from crm.models import Activity, Lead, Opportunity

register(DocType(
    slug="lead", model=Lead, module="crm",
    list_fields=["name", "organization", "phone", "source", "status", "sales_person", "created_at"],
    search_fields=["name", "organization", "phone", "email"],
    detail_template="crm/_activities.html",
))
register(DocType(
    slug="opportunity", model=Opportunity, module="crm",
    list_fields=["title", "customer", "lead", "stage", "expected_amount", "probability", "expected_close", "sales_person"],
    search_fields=["title", "customer__name", "lead__name"],
    detail_template="crm/_activities.html",
))
register(DocType(
    slug="activity", model=Activity, module="crm",
    list_fields=["date", "kind", "summary", "lead", "opportunity", "customer", "assigned_to", "done"],
    search_fields=["summary", "lead__name", "customer__name"],
))
