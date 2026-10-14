"""تنبيهات يومية لمديري الجمعيات: وثائق السيارات التي تنتهي خلال 30 يوماً، والصيانة الدورية المستحقة،
وطلبات الصيانة العاجلة والطارئة المفتوحة منذ أكثر من يومين (التنبيه نفسه لا يتكرر في اليوم نفسه).

python manage.py fleet_alerts
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Notification, OrganizationType
from core.notify import notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات الحركة والصيانة"

    def handle(self, *args, **options):
        from fleet.models import (EXPIRY_WARNING_DAYS, OPEN_MAINTENANCE, MaintenancePriority, MaintenanceRequest,
                                  Vehicle, VehicleStatus)

        today = timezone.localdate()
        start_of_day = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))
        sent = 0
        for company in Company.objects.filter(organization_type=OrganizationType.NONPROFIT):
            with tenant_context(company):
                def alert(title, url):
                    nonlocal sent
                    if not Notification.objects.filter(title=title, created_at__gte=start_of_day).exists():
                        notify_managers(company, title, url)
                        sent += 1

                limit = today + datetime.timedelta(days=EXPIRY_WARNING_DAYS)
                vehicles = Vehicle.objects.exclude(status=VehicleStatus.OUT)
                for vehicle in vehicles.filter(Vehicle.expiring_q(limit)):
                    for label, date in vehicle.expiring_documents:
                        state = "انتهت" if date < today else "تنتهي"
                        alert(f"{label} السيارة {vehicle.plate_number} {state} في {date:%Y-%m-%d}",
                              vehicle.get_absolute_url())
                for vehicle in vehicles:
                    if vehicle.service_due:
                        alert(f"الصيانة الدورية مستحقة للسيارة {vehicle.plate_number} (العداد {vehicle.odometer} كم، "
                              f"موعدها {vehicle.next_service_km} كم)", vehicle.get_absolute_url())
                stale = timezone.now() - datetime.timedelta(days=2)
                for item in MaintenanceRequest.objects.filter(
                        status__in=OPEN_MAINTENANCE, created_at__lt=stale,
                        priority__in=[MaintenancePriority.URGENT, MaintenancePriority.EMERGENCY]):
                    alert(f"طلب صيانة {item.get_priority_display()} مفتوح منذ أكثر من يومين: {item}",
                          item.get_absolute_url())
        self.stdout.write(f"أُرسل {sent} تنبيهاً.")
