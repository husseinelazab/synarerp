"""توليد استحقاقات الكفالات للشهر الحالي لكل الجمعيات، وتنبيه المديرين بالمتأخرات (يومياً من المجدول، ولا يتكرر).

python manage.py sponsorship_dues
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, OrganizationType
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "توليد استحقاقات الكفالات الشهرية"

    def handle(self, *args, **options):
        from django.urls import reverse

        from core.models import Notification
        from core.notify import notify_managers
        from funds.models import DueStatus, SponsorshipDue, generate_dues

        today = timezone.localdate()
        for company in Company.objects.filter(organization_type=OrganizationType.NONPROFIT):
            with tenant_context(company):
                created = generate_dues(today)
                overdue = SponsorshipDue.objects.filter(status__in=[DueStatus.PENDING, DueStatus.PARTIAL],
                                                        period__lt=today.replace(day=1)).count()
                title = f"استحقاقات كفالات متأخرة: {overdue} حتى {today:%Y-%m}"
                if overdue and not Notification.objects.filter(title=title).exists():
                    notify_managers(company, title, reverse("resources_dashboard"))
            if created:
                self.stdout.write(f"{company}: {created} استحقاق")
