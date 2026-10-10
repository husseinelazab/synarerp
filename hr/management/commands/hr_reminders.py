"""تنبيهات يومية لمديري كل شركة: الوثائق المنتهية أو القريبة من الانتهاء والطلبات المعلقة.

شغّلها يومياً (مثلاً عبر cron): python manage.py hr_reminders
"""
import datetime

from django.core.management.base import BaseCommand

from core.models import Company, Notification
from core.notify import notify_managers
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "إرسال تنبيهات الموارد البشرية (انتهاء الهويات والجوازات، والطلبات المعلقة)"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="التنبيه قبل الانتهاء بعدد أيام (افتراضي 30)")

    def handle(self, *args, **options):
        from django.urls import reverse

        from hr.models import DocStatus, Employee, EmployeeLoan, EmployeeStatus, LeaveApplication

        today = datetime.date.today()
        horizon = today + datetime.timedelta(days=options["days"])
        sent = 0
        for company in Company.objects.all():
            with tenant_context(company):
                for emp in Employee.objects.exclude(status=EmployeeStatus.LEFT):
                    for label, date in (("الهوية / الإقامة", emp.id_expiry_date), ("الجواز", emp.passport_expiry_date)):
                        if not date or date > horizon:
                            continue
                        state = "انتهت" if date < today else "تنتهي"
                        title = f"{state} {label} للموظف {emp.full_name} في {date:%Y-%m-%d}"
                        # تنبيه واحد لكل وثيقة وتاريخ انتهاء
                        if Notification.objects.filter(title=title).exists():
                            continue
                        notify_managers(company, title, emp.get_absolute_url())
                        sent += 1
                pending = (LeaveApplication.objects.filter(docstatus=DocStatus.DRAFT, requested_via_portal=True).count()
                           + EmployeeLoan.objects.filter(docstatus=DocStatus.DRAFT, requested_via_portal=True).count())
                if pending:
                    title = f"{pending} طلب من بوابة الموظفين بانتظار الاعتماد ({today:%Y-%m-%d})"
                    if not Notification.objects.filter(title=title).exists():
                        notify_managers(company, title, reverse("hr_dashboard"))
                        sent += 1
        self.stdout.write(self.style.SUCCESS(f"تم إرسال {sent} تنبيه."))
