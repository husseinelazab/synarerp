"""تنبيه يومي للمديرين بالأصناف التي نزلت إلى حد إعادة الطلب."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Notification
from core.notify import notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات حد إعادة الطلب"

    def handle(self, *args, **options):
        from stock.reorder import reorder_rows

        sent = 0
        for company in Company.objects.all():
            with tenant_context(company):
                rows = [r for r in reorder_rows() if r["below"]]
                if not rows:
                    continue
                names = "، ".join(r["item"].name for r in rows[:5])
                more = f" و{len(rows) - 5} غيرها" if len(rows) > 5 else ""
                title = f"{len(rows)} صنف نزل إلى حد إعادة الطلب: {names}{more}"[:250]
                if Notification.objects.filter(title=title, created_at__date=timezone.localdate()).exists():
                    continue
                notify_managers(company, title, "/reports/stock/reorder/")
                sent += 1
        self.stdout.write(f"أُرسل {sent} تنبيه.")
