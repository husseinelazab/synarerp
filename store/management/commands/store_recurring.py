"""خصم التبرعات الشهرية المستحقة بالبطاقات المحفوظة لدى ميسّر (يومياً).

python manage.py store_recurring
"""
from django.core.management.base import BaseCommand

from store.service import charge_due_recurring


class Command(BaseCommand):
    help = "خصم التبرعات الشهرية المستحقة"

    def handle(self, *args, **options):
        paid, failed = charge_due_recurring()
        self.stdout.write(f"التبرعات الشهرية: {paid} ناجحة، {failed} فاشلة")
