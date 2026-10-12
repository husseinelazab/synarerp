"""يومياً: تنبيه المسؤولين بمتابعات اليوم والمتأخرة (مرة لكل متابعة)."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تنبيهات متابعات إدارة العملاء"

    def handle(self, *args, **options):
        from core.notify import notify, notify_managers
        from crm.models import Activity

        count = 0
        for company in Company.objects.all():
            with tenant_context(company):
                for activity in Activity.objects.filter(done=False, reminded=False, date__lte=timezone.localdate()):
                    title = f"متابعة مستحقة: {activity} — {activity.regarding or ''}".strip(" —")
                    url = f"/app/activity/{activity.pk}/"
                    if activity.assigned_to_id:
                        notify(company, [activity.assigned_to], title, url)
                    else:
                        notify_managers(company, title, url)
                    activity.reminded = True
                    activity.save(update_fields=["reminded"])
                    count += 1
        self.stdout.write(f"تنبيهات المتابعات: {count}")
