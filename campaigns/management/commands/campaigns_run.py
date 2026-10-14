"""للمجدول اليومي/الدوري: إرسال الحملات المجدولة وتذكيرات الكفالات لكل جمعية فعّالة."""
from django.core.management.base import BaseCommand

from campaigns.service import campaigns_due_reminders, run_scheduled_campaigns


class Command(BaseCommand):
    help = "إرسال حملات التواصل المجدولة وتذكيرات الكفالات الآلية"

    def handle(self, *args, **options):
        from core.models import Company

        campaigns = reminders = 0
        for company in Company.objects.all():
            if not company.is_nonprofit:
                continue
            subscription = getattr(company, "subscription", None)
            if subscription is not None and not subscription.is_valid:
                continue
            try:
                campaigns += run_scheduled_campaigns(company)
                reminders += campaigns_due_reminders(company)
            except Exception as exc:  # شركة معطوبة لا توقف البقية
                self.stderr.write(f"{company}: {exc}")
        self.stdout.write(f"الحملات المرسلة: {campaigns}، التذكيرات: {reminders}")
