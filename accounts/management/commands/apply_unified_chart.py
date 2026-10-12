"""إضافة الحسابات الناقصة من دليل الحسابات السعودي الموحد لشركة أو لكل الشركات (دون تعديل الحسابات القائمة).

python manage.py apply_unified_chart [--company رقم]
"""
from django.core.management.base import BaseCommand

from core.models import Company
from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "تحديث دليل الحسابات إلى الدليل السعودي الموحد"

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int)

    def handle(self, *args, **options):
        from accounts.chart import apply_unified_chart

        companies = Company.objects.filter(pk=options["company"]) if options["company"] else Company.objects.all()
        for company in companies:
            with tenant_context(company):
                created, skipped = apply_unified_chart(company)
            self.stdout.write(f"{company}: أُضيف {created}" + (f"، تعذر {len(skipped)}" if skipped else ""))
