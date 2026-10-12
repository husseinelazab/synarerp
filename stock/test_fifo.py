from decimal import Decimal

from django.test import TestCase

from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Bin, Item, ItemGroup, StockEntry, Warehouse


class FifoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة FIFO", "fifo")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.wh = Warehouse.objects.first()
        self.item = Item.objects.create(code="FF", name="ذهب", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), valuation_method="fifo")

    def receive(self, qty, rate):
        e = StockEntry.objects.create(purpose="receipt", to_warehouse=self.wh)
        e.items.create(item=self.item, qty=qty, rate=rate)
        e.calculate_totals()
        e.save()
        e.submit()
        return e

    def sell(self, qty):
        inv = SalesInvoice.objects.create(customer=Customer.objects.get_or_create(name="ع")[0], warehouse=self.wh)
        inv.items.create(item=self.item, qty=qty, rate=500)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def cost(self, inv):
        from stock.models import StockLedgerEntry

        return -StockLedgerEntry.objects.get(voucher_type="sales-invoice", voucher_id=inv.pk).stock_value_difference

    def test_oldest_layers_consumed_first(self):
        self.receive(10, 100)
        self.receive(10, 200)
        self.assertEqual(self.cost(self.sell(15)), Decimal("2000"))  # 10×100 + 5×200
        b = Bin.objects.get(item=self.item)
        self.assertEqual((b.actual_qty, b.stock_value), (5, Decimal("1000")))
        self.assertEqual(self.cost(self.sell(5)), Decimal("1000"))

    def test_cancel_rebuilds_layers(self):
        self.receive(10, 100)
        second = self.receive(10, 200)
        sale = self.sell(5)
        sale.cancel()
        self.assertEqual(self.cost(self.sell(12)), Decimal("1400"))  # 10×100 + 2×200
        self.assertEqual(sum(Decimal(q) for q, _ in Bin.objects.get(item=self.item).fifo_queue), Decimal("8"))
        self.assertTrue(second.is_submitted)

    def test_switching_existing_stock_to_fifo(self):
        avg = Item.objects.create(code="AV", name="متوسط", item_group=ItemGroup.objects.first(), uom=UOM.objects.first())
        e = StockEntry.objects.create(purpose="receipt", to_warehouse=self.wh)
        e.items.create(item=avg, qty=10, rate=100)
        e.items.create(item=avg, qty=10, rate=300)
        e.calculate_totals(); e.save(); e.submit()
        avg.valuation_method = "fifo"
        avg.save()
        self.item = avg
        self.receive(10, 500)
        self.assertEqual(self.cost(self.sell(20)), Decimal("4000"))  # الرصيد القديم طبقة بمتوسطه 200
