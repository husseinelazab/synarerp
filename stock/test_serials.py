import datetime

from django.core.exceptions import ValidationError
from django.test import TestCase

from buying.models import PurchaseInvoice, Supplier
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, SerialNo, StockEntry, Warehouse


class SerialTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الجوالات", "serials")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.phone = Item.objects.create(code="PH", name="جوال", item_group=ItemGroup.objects.first(),
                                         uom=UOM.objects.first(), has_serial_no=True, warranty_days=365,
                                         selling_rate=2000)
        self.main, self.branch = Warehouse.objects.all()[:2]
        self.customer = Customer.objects.create(name="مشترٍ")

    def buy(self, serials, qty=None):
        inv = PurchaseInvoice.objects.create(supplier=Supplier.objects.get_or_create(name="مورد الجوالات")[0],
                                             warehouse=self.main)
        inv.items.create(item=self.phone, qty=qty or len(serials.split()), rate=1500, serial_nos=serials)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def sell(self, serials, qty=None, warehouse=None):
        inv = SalesInvoice.objects.create(customer=self.customer, warehouse=warehouse or self.main)
        inv.items.create(item=self.phone, qty=qty or len(serials.split()), rate=2000, serial_nos=serials)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def test_full_lifecycle(self):
        purchase = self.buy("IMEI1 IMEI2 IMEI3")
        self.assertEqual(SerialNo.objects.filter(status="in_stock", warehouse=self.main).count(), 3)
        sale = self.sell("IMEI2")
        s2 = SerialNo.objects.get(serial_no="IMEI2")
        self.assertEqual((s2.status, s2.customer), ("delivered", self.customer))
        self.assertEqual(s2.warranty_expiry, sale.posting_date + datetime.timedelta(days=365))
        self.assertTrue(s2.under_warranty)
        with self.assertRaises(ValidationError):  # بيع نفس القطعة مرتين
            self.sell("IMEI2")
        ret = sale.make_return()
        self.assertEqual(ret.items.get().serial_nos, "IMEI2")
        ret.submit()
        s2.refresh_from_db()
        self.assertEqual(s2.status, "in_stock")
        ret.cancel()
        sale.cancel()
        s2.refresh_from_db()
        self.assertEqual((s2.status, s2.warehouse), ("in_stock", self.main))
        self.assertEqual(purchase.name, s2.purchase_document)

    def test_count_must_match_and_duplicates(self):
        with self.assertRaises(ValidationError):
            self.buy("A1 A2", qty=3)
        with self.assertRaises(ValidationError):
            self.buy("B1 B1")
        self.buy("C1")
        with self.assertRaises(ValidationError):  # موجود بالفعل في المخزون
            self.buy("C1")

    def test_transfer_moves_serials(self):
        self.buy("T1 T2")
        entry = StockEntry.objects.create(purpose="transfer", from_warehouse=self.main, to_warehouse=self.branch)
        entry.items.create(item=self.phone, qty=1, serial_nos="T1")
        entry.submit()
        self.assertEqual(SerialNo.objects.get(serial_no="T1").warehouse, self.branch)
        with self.assertRaises(ValidationError):  # T1 لم يعد في المستودع الرئيسي
            self.sell("T1")
        self.sell("T1", warehouse=self.branch)

    def test_pages(self):
        self.buy("P1")
        self.client.force_login(self.user)
        serial = SerialNo.objects.get()
        self.assertContains(self.client.get(f"/app/serial-no/?q=P1"), "P1")
        self.assertContains(self.client.get(serial.get_absolute_url() if hasattr(serial, "get_absolute_url")
                                            else f"/app/serial-no/{serial.pk}/"), "سجل الحركة")
