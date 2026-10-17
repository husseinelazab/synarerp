"""Read-only preflight before enforcing barcode uniqueness in existing installations."""
from django.core.management.base import BaseCommand, CommandError
from stock.models import Item, ItemUOM


class Command(BaseCommand):
    help = 'فحص تكرار باركود الأصناف ووحداتها داخل كل منشأة، دون تعديل البيانات.'

    def handle(self, *args, **options):
        seen, duplicates = {}, []
        for model in (Item, ItemUOM):
            for row in model.all_objects.exclude(barcode='').values('id', 'company_id', 'barcode').iterator():
                code = row['barcode'].strip()
                canonical = '0' + code if code.isdigit() and len(code) == 12 else code
                if not code:
                    continue
                key = (row['company_id'], canonical)
                label = f'{model._meta.label} #{row["id"]}'
                if key in seen:
                    duplicates.append(f'المنشأة #{row["company_id"]}: {seen[key]} / {label}')
                else:
                    seen[key] = label
        if duplicates:
            for line in duplicates:
                self.stdout.write(line)
            raise CommandError(f'وُجد {len(duplicates)} تعارض. صحح الباركود في السجلات المحددة؛ لم تُحذف أو تُعدّل بيانات.')
        self.stdout.write(self.style.SUCCESS('لا توجد باركودات مكررة داخل أي منشأة.'))
