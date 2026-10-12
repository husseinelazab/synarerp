"""تنبيهات المحاسبة اليومية (من المجدول): الشيكات المستحقة خلال 3 أيام أو المتأخرة.

python manage.py accounts_alerts
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيه المديرين بالشيكات المستحقة والمتأخرة"

    def handle(self, *args, **options):
        from accounts.models import Cheque, ChequeStatus
        from core.models import Notification
        from core.notify import notify_managers

        today = timezone.localdate()
        sent = 0
        for company in Company.objects.all():
            with tenant_context(company):
                due = Cheque.objects.filter(docstatus=1, cheque_date__lte=today + datetime.timedelta(days=3),
                                            status__in=[ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED,
                                                        ChequeStatus.ISSUED])
                count = due.count()
                if not count:
                    continue
                title = f"{count} شيك مستحق خلال 3 أيام أو متأخر — {today:%Y-%m-%d}"
                if Notification.all_objects.filter(company=company, title=title).exists():
                    continue  # مرة واحدة يومياً
                total = sum(c.amount for c in due)
                notify_managers(company, title, "/reports/accounts/cheques/",
                                body=f"{title}\nالإجمالي: {total:,.2f}")
                sent += 1
        self.stdout.write(f"تنبيهات الشيكات: {sent} شركة.")
