"""يومياً: تنبيه الفنيين بالزيارات المستحقة، والمديرين بالتذاكر المتأخرة عن مستوى الخدمة والعقود القريبة من الانتهاء."""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات الصيانة وخدمة العملاء"

    def handle(self, *args, **options):
        from core.models import Notification
        from core.notify import notify, notify_managers
        from support.models import MaintenanceContract, MaintenanceVisit, ServiceTicket, TicketStatus

        today = timezone.localdate()
        for company in Company.objects.all():
            with tenant_context(company):
                for visit in MaintenanceVisit.objects.filter(done=False, reminded=False, scheduled_date__lte=today):
                    title = f"زيارة صيانة مستحقة: {visit} — {visit.purpose}"
                    url = f"/app/maintenance-visit/{visit.pk}/"
                    notify(company, [visit.technician], title, url) if visit.technician_id else \
                        notify_managers(company, title, url)
                    visit.reminded = True
                    visit.save(update_fields=["reminded"])
                late = [t for t in ServiceTicket.objects.exclude(status__in=[TicketStatus.RESOLVED, TicketStatus.CLOSED])
                        if t.is_overdue]
                title = f"{len(late)} تذكرة متأخرة عن موعد الحل — {today:%Y-%m-%d}"
                if late and not Notification.all_objects.filter(company=company, title=title).exists():
                    notify_managers(company, title, "/support/")
                for contract in MaintenanceContract.objects.filter(status="active",
                                                                   end_date=today + datetime.timedelta(days=30)):
                    notify_managers(company, f"عقد الصيانة «{contract}» ينتهي بعد 30 يوماً — جدّده مع {contract.customer}",
                                    f"/app/maintenance-contract/{contract.pk}/")
        self.stdout.write("تنبيهات الصيانة: تم")
