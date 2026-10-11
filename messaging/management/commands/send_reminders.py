"""تذكير العملاء بالفواتير المتأخرة (يومياً من المجدول) لمن فعّل التذكير الآلي.

python manage.py send_reminders
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "إرسال تذكيرات الفواتير المتأخرة عبر SMS أو واتساب"

    def handle(self, *args, **options):
        from messaging.models import MessageLog, MessagingSettings
        from messaging.service import compose, invoice_context, send
        from selling.models import SalesInvoice

        today = timezone.localdate()
        sent = 0
        for company in Company.objects.all():
            cfg = MessagingSettings.all_objects.filter(company=company).first()
            if cfg is None or not cfg.active or not cfg.auto_overdue:
                continue
            with tenant_context(company):
                since = timezone.now() - datetime.timedelta(days=max(cfg.overdue_every_days, 1))
                for inv in (SalesInvoice.objects.filter(docstatus=1, is_return=False, outstanding_amount__gt=0,
                                                        due_date__lt=today).select_related("customer")):
                    if not inv.customer.phone:
                        continue
                    if MessageLog.objects.filter(reference=inv.name, event="overdue", created_at__gte=since).exists():
                        continue
                    try:
                        send(company, inv.customer.phone, compose(company, "overdue", **invoice_context(inv)),
                             event="overdue", reference=inv.name, customer=inv.customer, mode="provider")
                        sent += 1
                    except ValueError:
                        continue
        self.stdout.write(f"أُرسل {sent} تذكير.")
