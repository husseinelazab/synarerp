"""إرسال كميات المخزون للمتاجر الإلكترونية التي فعّلت المزامنة (كل ساعة من المجدول).

python manage.py sync_store_stock
"""
from django.core.management.base import BaseCommand

from core.tenancy import tenant_context


class Command(BaseCommand):
    help = "مزامنة كميات المخزون مع متاجر سلة وزد"

    def handle(self, *args, **options):
        from ecommerce.api import ApiError
        from ecommerce.models import StoreConnection
        from ecommerce.sync import sync_stock

        for connection in StoreConnection.all_objects.filter(enabled=True, sync_stock=True).select_related("company"):
            if not connection.has_api_access:
                continue
            with tenant_context(connection.company):
                try:
                    count = sync_stock(connection)
                    self.stdout.write(f"{connection}: {count}")
                except ApiError as exc:  # متجر متعطل لا يوقف البقية؛ السبب يظهر في صفحة المتجر
                    self.stderr.write(f"{connection}: {exc}")
