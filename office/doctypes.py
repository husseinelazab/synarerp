from core.registry import DocType, register
from office.forms import CorrespondenceForm
from office.models import (AdminRequest, AdminRequestType, ArchiveCategory, ArchiveDocument, Circular,
                           Correspondence)

register(DocType(
    slug="correspondence", model=Correspondence, module="documents", form_class=CorrespondenceForm,
    list_fields=["number", "direction", "date", "subject", "party", "priority", "assigned_to", "status"],
    search_fields=["number", "subject", "party", "external_reference", "body"],
    detail_template="office/_correspondence_detail.html", type_field="direction",
    type_titles={"incoming": ("معاملة واردة", "الوارد"), "outgoing": ("معاملة صادرة", "الصادر"),
                 "internal": ("معاملة داخلية", "المعاملات الداخلية")},
))
register(DocType(
    slug="archive-category", model=ArchiveCategory, module="documents", list_fields=["name", "parent"],
    search_fields=["name"],
))
register(DocType(
    slug="archive-document", model=ArchiveDocument, module="documents",
    list_fields=["title", "category", "document_date", "reference", "expiry_date", "physical_location"],
    search_fields=["title", "reference", "keywords", "notes"],
))
register(DocType(
    slug="admin-request-type", model=AdminRequestType, module="office", list_fields=["name", "description"],
    search_fields=["name"],
))
register(DocType(
    slug="admin-request", model=AdminRequest, module="office",
    list_fields=["name", "posting_date", "request_type", "subject", "amount", "done"],
    form_fields=["request_type", "subject", "details", "amount", "needed_by", "posting_date"],
    search_fields=["name", "subject", "details"],
))
register(DocType(
    slug="circular", model=Circular, module="office", list_fields=["title", "date", "requires_ack", "expires_on"],
    search_fields=["title", "body"], detail_template="office/_circular_detail.html",
))
