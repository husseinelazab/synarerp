from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, GLEntry
from buying.models import LandedCostVoucher, PurchaseReceipt, Supplier
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse


def balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class LandedCostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الاستيراد", "importer")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        group, uom = ItemGroup.objects.first(), UOM.objects.first()
        self.a = Item.objects.create(code="LA", name="ثلاجة", item_group=group, uom=uom, selling_rate=2000)
        self.b = Item.objects.create(code="LB", name="مكيف", item_group=group, uom=uom, selling_rate=3000)
        self.warehouse = Warehouse.objects.first()
        self.supplier = Supplier.objects.create(name="مورد خارجي")
        self.receipt = PurchaseReceipt.objects.create(supplier=self.supplier, warehouse=self.warehouse)
        self.receipt.items.create(item=self.a, qty=10, rate=1000)   # 10,000
        self.receipt.items.create(item=self.b, qty=5, rate=6000)    # 30,000
        self.receipt.calculate_totals()
        self.receipt.save()
        self.receipt.submit()
        self.freight = Account.objects.get(number="2104")  # مصروفات مستحقة (فاتورة شركة الشحن)

    def voucher(self, amount=4000, basis="amount"):
        lcv = LandedCostVoucher.objects.create(purchase_receipt=self.receipt, basis=basis)
        lcv.charges.create(description="شحن وجمارك", account=self.freight, amount=amount)
        lcv.calculate_totals()
        lcv.save()
        return lcv

    def test_charges_raise_valuation_by_value(self):
        lcv = self.voucher()
        lcv.submit()
        # 4,000 موزعة بالقيمة: 1,000 للثلاجات (ربع القيمة) و3,000 للمكيفات
        self.assertEqual(Bin.objects.get(item=self.a).valuation_rate, Decimal("1100"))
        self.assertEqual(Bin.objects.get(item=self.b).valuation_rate, Decimal("6600"))
        self.assertEqual(balance(account=self.company.default_stock_account), Decimal("44000"))
        self.assertEqual(balance(account=self.freight), Decimal("-4000"))  # دائن بالتكلفة
        self.assertEqual(lcv.capitalized, Decimal("4000"))
        lcv.cancel()
        self.assertEqual(Bin.objects.get(item=self.a).valuation_rate, Decimal("1000"))
        self.assertEqual(balance(account=self.company.default_stock_account), Decimal("40000"))

    def test_by_quantity(self):
        self.voucher(1500, basis="qty").submit()
        self.assertEqual(Bin.objects.get(item=self.a).valuation_rate, Decimal("1100"))  # 1,000 / 10
        self.assertEqual(Bin.objects.get(item=self.b).valuation_rate, Decimal("6100"))  # 500 / 5

    def test_sold_portion_goes_to_cogs_and_cancel_blocked(self):
        inv = SalesInvoice.objects.create(customer=Customer.objects.create(name="مشترٍ"), warehouse=self.warehouse)
        inv.items.create(item=self.a, qty=4, rate=2000)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        lcv = self.voucher()
        lcv.submit()
        # حصة الثلاجات 1,000: 6 باقية تحمل 600، و4 مباعة تذهب 400 لتكلفة المبيعات
        self.assertEqual(lcv.expensed, Decimal("400"))
        self.assertEqual(Bin.objects.get(item=self.a).stock_value, Decimal("6600"))
        self.assertEqual(balance(account=self.company.default_cogs_account), Decimal("4400"))
        inv2 = SalesInvoice.objects.create(customer=inv.customer, warehouse=self.warehouse)
        inv2.items.create(item=self.a, qty=1, rate=2000)
        inv2.calculate_totals()
        inv2.save()
        inv2.submit()
        self.assertEqual(balance(account=self.company.default_cogs_account), Decimal("5500"))  # بالمتوسط الجديد
        with self.assertRaises(ValidationError):
            lcv.cancel()

    def test_source_cannot_be_cancelled_while_loaded(self):
        self.voucher().submit()
        with self.assertRaises(ValidationError):
            self.receipt.cancel()

    def test_validation(self):
        lcv = LandedCostVoucher.objects.create(purchase_receipt=self.receipt)
        with self.assertRaises(ValidationError):  # بلا تكاليف
            lcv.submit()

    def test_pages_render(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.receipt.get_absolute_url()), "تحميل تكاليف الشحن والجمارك")
        page = self.client.get(f"/app/landed-cost/new/?purchase_receipt={self.receipt.pk}")
        self.assertEqual(page.status_code, 200)
        lcv = self.voucher()
        lcv.submit()
        self.assertEqual(self.client.get(lcv.get_absolute_url()).status_code, 200)
