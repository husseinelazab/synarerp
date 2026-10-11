from django.core.management.base import BaseCommand, CommandError

from core.company_export import export_filename, write_export
from core.models import Company


class Command(BaseCommand):
    help = "تصدير كامل بيانات شركة واحدة في ملف ZIP (مفيد أيضاً بعد استعادة نسخة احتياطية في قاعدة مؤقتة)"

    def add_arguments(self, parser):
        parser.add_argument("company", help="رقم الشركة أو اسمها")
        parser.add_argument("--out", help="مسار الملف الناتج، أو - للمخرج القياسي")

    def handle(self, company, out=None, **options):
        found = Company.objects.filter(pk=company).first() if company.isdigit() else None
        found = found or Company.objects.filter(name=company).first()
        if found is None:
            raise CommandError(f"لا توجد شركة بالرقم أو الاسم: {company}")
        path = out or export_filename(found)
        if path == "-":  # docker compose run ... export_company 5 --out - > company.zip
            import sys

            manifest = write_export(found, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        else:
            with open(path, "wb") as fh:
                manifest = write_export(found, fh)
        total = sum(t["count"] for t in manifest)
        self.stderr.write(f"تم تصدير {found.name}: {len(manifest)} جدول و{total} سجل ← {path}")
