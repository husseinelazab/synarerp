from core.registry import DocType, register
from portal.models import ProfileUpdateRequest, ServiceRequest

register(DocType(
    slug="portal-profile-update", model=ProfileUpdateRequest, module="beneficiaries",
    list_fields=["number", "beneficiary", "created_at", "phone", "city", "district", "housing_type", "status"],
    form_fields=["beneficiary", "phone", "city", "district", "housing_type", "note", "reply"],
    search_fields=["number", "beneficiary__full_name", "beneficiary__national_id", "beneficiary__file_number"],
    detail_template="portal/_update_detail.html",
))
register(DocType(
    slug="portal-service-request", model=ServiceRequest, module="beneficiaries",
    list_fields=["number", "beneficiary", "created_at", "kind", "status", "updated_at"],
    form_fields=["beneficiary", "kind", "description", "reply"],
    search_fields=["number", "description", "beneficiary__full_name", "beneficiary__national_id",
                   "beneficiary__file_number"],
))
