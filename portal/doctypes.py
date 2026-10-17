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

from portal.models import ServiceSlot, ServiceBooking
register(DocType(slug="service-slot",model=ServiceSlot,module="beneficiaries",list_fields=["title","starts_at","ends_at","capacity","officer","published"],search_fields=["title","location"]))
register(DocType(slug="service-booking",model=ServiceBooking,module="beneficiaries",read_only=True,list_fields=["slot","beneficiary","status","created_at"],search_fields=["slot__title","beneficiary__full_name","beneficiary__file_number"],detail_template="portal/_booking_detail.html"))
