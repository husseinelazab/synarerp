"""تنبيه يومي للمديرين بالتشغيلات المنتهية أو القريبة من الانتهاء التي ما زال لها رصيد.

python manage.py expiry_alerts --days 30
"""
import datetime

from django.core.management.base import BaseCommand
from django.db.models import Sum

from core.models import Company, Notification
from core.notify import notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات صلاحية التشغيلات (المنتهية والقريبة من الانتهاء)"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="التنبيه قبل الانتهاء بعدد أيام (افتراضي 30)")

    def handle(self, *args, **options):
        from stock.models import Batch

        today = datetime.date.today()
        horizon = today + datetime.timedelta(days=options["days"])
        sent = 0
        for company in Company.objects.all():
            with tenant_context(company):
                batches = (Batch.objects.filter(expiry_date__isnull=False, expiry_date__lte=horizon)
                           .annotate(stock=Sum("bins__qty")).filter(stock__gt=0).select_related("item"))
                expired = [b for b in batches if b.expiry_date < today]
                near = [b for b in batches if b.expiry_date >= today]
                for group, label, status in ((expired, "منتهية الصلاحية وما زال لها رصيد", "expired"),
                                             (near, f"تنتهي خلال {options['days']} يوماً", "near")):
                    if not group:
                        continue
                    names = "، ".join(f"{b.item.name} ({b.batch_no})" for b in group[:4])
                    more = f" و{len(group) - 4} غيرها" if len(group) > 4 else ""
                    title = f"{len(group)} تشغيلة {label}: {names}{more}"[:250]
                    # تنبيه واحد في اليوم لكل نوع
                    if Notification.objects.filter(title=title, created_at__date=today).exists():
                        continue
                    notify_managers(company, title, f"/reports/stock/batches/?status={status}")
                    sent += 1
        self.stdout.write(f"أُرسل {sent} تنبيه صلاحية.")
