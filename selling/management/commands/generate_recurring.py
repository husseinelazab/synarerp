"""إنشاء الفواتير المتكررة المستحقة (يومياً من المجدول).

python manage.py generate_recurring
"""
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "إنشاء فواتير المبيعات المتكررة التي حلّ موعدها"

    def handle(self, *args, **options):
        from core.notify import notify_managers
        from selling.models import RecurringInvoice

        today = timezone.localdate()
        total = 0
        for company in Company.objects.all():
            with tenant_context(company):
                for recurring in RecurringInvoice.objects.filter(enabled=True, next_date__lte=today):
                    try:
                        created = recurring.generate_due(today)
                    except Exception as exc:  # noqa: BLE001 — عقد معطوب لا يوقف بقية العقود
                        RecurringInvoice.objects.filter(pk=recurring.pk).update(last_error=str(exc)[:255])
                        notify_managers(company, f"تعذر إنشاء الفاتورة المتكررة «{recurring.name}»: {exc}",
                                        reverse("doc_detail", args=["recurring-invoice", recurring.pk]))
                        continue
                    total += len(created)
                    drafts = [inv for inv in created if inv.is_draft]
                    if drafts:
                        notify_managers(company, f"فواتير متكررة بانتظار المراجعة والاعتماد: «{recurring.name}» "
                                                 f"({len(drafts)})" + (f" — {recurring.last_error}" if recurring.last_error else ""),
                                        drafts[-1].get_absolute_url())
        self.stdout.write(f"أُنشئت {total} فاتورة متكررة.")
