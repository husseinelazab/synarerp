"""تنبيهات التميز المؤسسي (يومياً، دون تكرار):
- مسؤولو المبادرات والمديرون: مبادرات تنتهي خلال 14 يوماً ولم تكتمل، أو تأخرت عن موعدها.
- مسؤولو المؤشرات (أو المديرون إن لم يُحدد مسؤول): مؤشرات الخطط المعتمدة التي لم تُسجل قراءتها خلال دورة القياس.

python manage.py excellence_alerts
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Company, Notification, OrganizationType
from core.notify import notify, notify_managers
from core.tenancy import tenant_context

DAYS_AHEAD = 14


def _once(company, users, title, url, managers=False):
    """يُرسل التنبيه مرة واحدة فقط بعنوانه."""
    title = title[:255]
    if Notification.objects.filter(title=title).exists():
        return 0
    if users:
        notify(company, users, title, url)
    if managers:
        notify_managers(company, title, url)
    return 1


class Command(BaseCommand):
    help = "تنبيهات المبادرات المتأخرة أو القريبة من موعدها ومؤشرات الأداء غير المقيسة"

    def handle(self, *args, **options):
        from excellence.models import KPI, Initiative, InitiativeStatus, PlanStatus

        today = timezone.localdate()
        sent = 0
        for company in Company.objects.filter(organization_type=OrganizationType.NONPROFIT):
            with tenant_context(company):
                initiatives = (Initiative.objects.select_related("owner", "goal")
                               .exclude(status__in=[InitiativeStatus.COMPLETED, InitiativeStatus.CANCELLED])
                               .filter(progress__lt=100, end_date__lte=today + datetime.timedelta(days=DAYS_AHEAD)))
                for initiative in initiatives:
                    if initiative.end_date < today:
                        title = (f"مبادرة متأخرة: «{initiative.title}» انتهى موعدها في {initiative.end_date:%Y-%m-%d} "
                                 f"والإنجاز {initiative.progress}%")
                    else:
                        title = (f"مبادرة «{initiative.title}» تنتهي في {initiative.end_date:%Y-%m-%d} "
                                 f"والإنجاز {initiative.progress}%")
                    sent += _once(company, [initiative.owner], title, initiative.get_absolute_url(), managers=True)
                kpis = (KPI.objects.filter(goal__plan__status=PlanStatus.APPROVED)
                        .select_related("owner").prefetch_related("readings"))
                for kpi in kpis:
                    if not kpi.is_stale(today):
                        continue
                    last = kpi.latest_reading
                    since = f"آخر قراءة {last.period_end:%Y-%m-%d}" if last else "لا توجد قراءات"
                    title = f"مؤشر الأداء «{kpi.name}» ({kpi.get_frequency_display()}) يحتاج قراءة جديدة — {since}"
                    sent += _once(company, [kpi.owner] if kpi.owner else [], title, kpi.get_absolute_url(),
                                  managers=kpi.owner is None)
        self.stdout.write(f"excellence_alerts: {sent} تنبيه")
