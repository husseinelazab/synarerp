from core.registry import DocType, register
from fleet.forms import VehicleRequestForm
from fleet.models import FuelLog, MaintenanceRequest, Vehicle, VehicleRequest

register(DocType(
    slug="vehicle", model=Vehicle, module="fleet",
    list_fields=["plate_number", "make", "model", "vehicle_type", "driver", "odometer", "status"],
    search_fields=["plate_number", "make", "model", "color", "driver__full_name"],
    detail_template="fleet/_vehicle_detail.html",
))
register(DocType(
    slug="vehicle-request", model=VehicleRequest, module="fleet", form_class=VehicleRequestForm,
    list_fields=["name", "posting_date", "purpose", "destination", "vehicle", "passengers", "created_by", "status"],
    search_fields=["name", "purpose", "destination", "vehicle__plate_number"],
    detail_template="fleet/_vehicle_request_detail.html", detail_exclude=["start_at", "end_at"],
))
register(DocType(
    slug="maintenance-request", model=MaintenanceRequest, module="fleet",
    list_fields=["number", "date", "target", "vehicle", "facility", "category", "priority", "status", "cost"],
    search_fields=["number", "facility", "description", "vendor", "vehicle__plate_number"],
))
register(DocType(
    slug="fuel-log", model=FuelLog, module="fleet",
    list_fields=["date", "vehicle", "liters", "amount", "odometer", "station"],
    search_fields=["vehicle__plate_number", "station"],
))
