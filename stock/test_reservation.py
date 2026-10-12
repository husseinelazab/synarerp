from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Company
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice, SalesOrder
from stock.models import UOM, Item, ItemGroup, OpeningStock, Warehouse
from stock.reservation import reserved_map


class ReservationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الحجز", "reserve")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.wh = Warehouse.objects.first()
        self.item = Item.objects.create(code="RS", name="مكيف", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), selling_rate=100)
        stock = OpeningStock.objects.create(warehouse=self.wh)
        stock.items.create(item=self.item, qty=10, rate=50)
        stock.submit()
        self.customer = Customer.objects.create(name="عميل الحجز")

    def order(self, qty):
        so = SalesOrder.objects.create(customer=self.customer, warehouse=self.wh)
        so.items.create(item=self.item, qty=qty, rate=100)
        so.calculate_totals()
        so.save()
        so.submit()
        return so

    def direct_sale(self, qty):
        inv = SalesInvoice.objects.create(customer=self.customer, warehouse=self.wh)
        inv.items.create(item=self.item, qty=qty, rate=100)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def test_reserved_and_protection(self):
        so = self.order(7)
        self.assertEqual(reserved_map()[(self.item.pk, self.wh.pk)], 7)
        self.direct_sale(2)  # بلا حماية: مسموح
        Company.objects.filter(pk=self.company.pk).update(protect_reserved_stock=True)
        self.company.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.direct_sale(2)  # المتاح 8 - 7 = 1
        self.direct_sale(1)
        inv = so.make_invoice()  # البيع من الأمر نفسه يستهلك حجزه
        inv.submit()
        self.assertEqual(reserved_map().get((self.item.pk, self.wh.pk), 0), 0)

    def test_closed_order_releases(self):
        so = self.order(5)
        so.close_order()
        self.assertFalse(reserved_map())

    def test_report_columns(self):
        self.order(4)
        self.client.force_login(self.user)
        page = self.client.get("/reports/stock/balance/")
        row = [b for b in page.context["bins"] if b.item_id == self.item.pk][0]
        self.assertEqual((row.reserved_qty, row.available_qty), (4, 6))
