"""تنبيهات يومية: المعاملات المتأخرة لمن هي لديه، والوثائق المؤرشفة (تراخيص وعقود) التي تنتهي خلال 30 يوماً للمديرين.

python manage.py office_alerts
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Notification, OrganizationType
from core.notify import notify, notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات الصادر والوارد والأرشيف"

    def handle(self, *args, **options):
        from office.models import ArchiveDocument, Correspondence, CorrespondenceStatus

        today = timezone.localdate()
        for company in Company.objects.filter(organization_type=OrganizationType.NONPROFIT):
            with tenant_context(company):
                for item in Correspondence.objects.exclude(status=CorrespondenceStatus.CLOSED).filter(
                        due_date__lt=today, assigned_to__isnull=False):
                    title = f"معاملة متأخرة لديك: {item}"
                    if not Notification.objects.filter(title=title, user=item.assigned_to).exists():
                        notify(company, [item.assigned_to], title, item.get_absolute_url())
                for doc in ArchiveDocument.objects.filter(expiry_date__isnull=False,
                                                          expiry_date__lte=today + timezone.timedelta(days=30)):
                    title = f"«{doc}» تنتهي في {doc.expiry_date:%Y-%m-%d}"
                    if not Notification.objects.filter(title=title).exists():
                        notify_managers(company, title, doc.get_absolute_url())
