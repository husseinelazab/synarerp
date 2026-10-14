"""تنبيهات يومية للمديرين: الشراكات السارية التي تنتهي خلال 30 يوماً، ومتابعات الزيارات المستحقة اليوم أو المتأخرة.

python manage.py relations_alerts
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Notification, OrganizationType
from core.notify import notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات الشراكات قريبة الانتهاء ومتابعات الزيارات"

    def handle(self, *args, **options):
        from relations.models import Partnership, PartnershipStatus, Visit, VisitStatus

        today = timezone.localdate()
        sent = 0
        for company in Company.objects.filter(organization_type=OrganizationType.NONPROFIT):
            with tenant_context(company):
                alerts = []
                for partnership in Partnership.objects.filter(status=PartnershipStatus.ACTIVE, end_date__isnull=False,
                                                              end_date__lte=today + datetime.timedelta(days=30)):
                    alerts.append((f"الشراكة مع «{partnership.partner}» تنتهي في {partnership.end_date:%Y-%m-%d}",
                                   partnership.get_absolute_url()))
                for visit in Visit.objects.exclude(status=VisitStatus.CANCELLED).filter(follow_up_date__lte=today):
                    alerts.append((f"متابعة مستحقة ({visit.follow_up_date:%Y-%m-%d}) لزيارة {visit.entity}",
                                   visit.get_absolute_url()))
                for title, url in alerts:
                    # التاريخ جزء من العنوان، فلا يتكرر التنبيه نفسه إلا إن تغير الموعد
                    if not Notification.objects.filter(title=title, created_at__date=today).exists():
                        notify_managers(company, title, url)
                        sent += 1
        self.stdout.write(f"تم إرسال {sent} تنبيه.")
