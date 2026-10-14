"""يُرجع أرصدة القسائم الشرائية المنتهية إلى مصروف برامجها وصناديقها (يومياً من المجدول)."""
from django.core.management.base import BaseCommand

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "إرجاع أرصدة القسائم الشرائية المنتهية"

    def handle(self, *args, **options):
        from beneficiaries.vouchers import expire_due

        total = 0
        for company in Company.objects.filter(organization_type="nonprofit"):
            with tenant_context(company):
                try:
                    total += expire_due(company)
                except Exception as exc:  # فشل جمعية لا يوقف البقية
                    self.stderr.write(f"{company}: {exc}")
        self.stdout.write(f"أُرجعت أرصدة {total} قسيمة منتهية.")
