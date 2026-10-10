from django.core.management.base import BaseCommand

from core.tenancy import tenant_context
from einvoice.models import EInvoiceSettings
from einvoice.services import submit_pending


class Command(BaseCommand):
    help = "إعادة إرسال الفواتير الإلكترونية المعلقة أو التي فشل إرسالها لمنصة فاتورة (يُجدول كل ساعة مثلاً)."

    def handle(self, *args, **options):
        for settings in EInvoiceSettings.all_objects.filter(enabled=True).select_related("company"):
            with tenant_context(settings.company):
                results = submit_pending(settings.company)
            accepted = sum(1 for e in results if e.is_final)
            self.stdout.write(f"{settings.company}: أُرسلت {len(results)}، قُبلت {accepted}")
