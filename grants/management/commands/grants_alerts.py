"""تنبيه مديري الجمعيات بتقارير المانحين المستحقة/المتأخرة ودفعات المنح المتأخرة.

python manage.py grants_alerts
"""
from django.core.management.base import BaseCommand

from core.models import Company, OrganizationType
from grants.alerts import grants_alerts


class Command(BaseCommand):
    help = "تنبيهات المنح"

    def handle(self, *args, **options):
        total = sum(grants_alerts(company) for company in
                    Company.objects.filter(organization_type=OrganizationType.NONPROFIT))
        self.stdout.write(f"أُرسل {total} تنبيه.")
