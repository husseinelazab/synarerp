"""تنبيه مديري الجمعيات بأقساط الإيجار المتأخرة وعقود الأوقاف التي تنتهي خلال 60 يوماً (يومياً، دون تكرار).

python manage.py waqf_alerts
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Notification, OrganizationType
from core.notify import notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات الأوقاف"

    def handle(self, *args, **options):
        from awqaf.models import InstallmentStatus, LeaseStatus, RentInstallment, WaqfLease

        today = timezone.localdate()
        for company in Company.objects.filter(organization_type=OrganizationType.NONPROFIT):
            with tenant_context(company):
                for lease in WaqfLease.objects.filter(status=LeaseStatus.ACTIVE, end_date__gte=today,
                                                      end_date__lte=today + timezone.timedelta(days=60)):
                    title = f"عقد إيجار الوقف «{lease.unit}» ({lease.tenant_name}) ينتهي في {lease.end_date:%Y-%m-%d}"
                    if not Notification.objects.filter(title=title).exists():
                        notify_managers(company, title, lease.get_absolute_url())
                for installment in RentInstallment.objects.filter(
                        status__in=[InstallmentStatus.PENDING, InstallmentStatus.PARTIAL], due_date__lt=today):
                    title = f"قسط إيجار متأخر: {installment} على {installment.lease.tenant_name}"
                    if not Notification.objects.filter(title=title).exists():
                        notify_managers(company, title, installment.get_absolute_url())
