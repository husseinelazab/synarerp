from core.registry import DocType, register
from support.models import MaintenanceContract, MaintenanceVisit, ServiceTicket

register(DocType(
    slug="service-ticket", model=ServiceTicket, module="support",
    list_fields=["subject", "customer", "kind", "priority", "status", "assigned_to", "opened_at", "covered"],
    search_fields=["subject", "customer__name", "serial_no__serial_no", "contact_phone"],
))
register(DocType(
    slug="maintenance-contract", model=MaintenanceContract, module="support",
    list_fields=["name", "customer", "start_date", "end_date", "amount", "visits_per_year", "status"],
    search_fields=["name", "customer__name"],
))
register(DocType(
    slug="maintenance-visit", model=MaintenanceVisit, module="support",
    list_fields=["scheduled_date", "customer", "purpose", "technician", "contract", "done"],
    search_fields=["customer__name", "purpose"],
))
