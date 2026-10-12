"""يومياً من المجدول: إنشاء المستندات المتكررة المستحقة، وترحيل أقساط المصروفات المقدمة والإيرادات المؤجلة.

python manage.py run_recurring
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "المستندات المتكررة وأقساط المقدم/المؤجل المستحقة"

    def handle(self, *args, **options):
        from accounts.models import DeferralSchedule
        from core.notify import notify_managers
        from core.recurring import run_due

        today = timezone.localdate()
        results = run_due(today)
        posted = 0
        for company in Company.objects.all():
            with tenant_context(company):
                for schedule in DeferralSchedule.objects.filter(docstatus=1, entries__journal_entry__isnull=True,
                                                                entries__posting_date__lte=today).distinct():
                    try:
                        posted += schedule.post_due(until=today)
                    except ValidationError as exc:
                        notify_managers(company, f"تعذر ترحيل أقساط {schedule}: {' '.join(exc.messages)}",
                                        schedule.get_absolute_url())
        self.stdout.write(f"مستندات متكررة: {len(results)} · أقساط مرحلة: {posted}")
