from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, GLEntry
from buying.models import LandedCost, PurchaseInvoice, Supplier
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
        self.wh = Warehouse.objects.first()
        group, uom = ItemGroup.objects.first(), UOM.objects.first()
        self.a = Item.objects.create(code="LA", name="مكيف", item_group=group, uom=uom)
        self.b = Item.objects.create(code="LB", name="ثلاجة", item_group=group, uom=uom)
        self.supplier = Supplier.objects.create(name="مورد صيني")
        self.shipper = Supplier.objects.create(name="شركة الشحن")
        self.invoice = PurchaseInvoice.objects.create(supplier=self.supplier, warehouse=self.wh)
        self.invoice.items.create(item=self.a, qty=10, rate=100)
        self.invoice.items.create(item=self.b, qty=5, rate=200)
        self.invoice.submit()
        sale = SalesInvoice.objects.create(customer=Customer.objects.create(name="عميل"), warehouse=self.wh)
        sale.items.create(item=self.a, qty=4, rate=300)
        sale.submit()

    def landed(self, allocation="value", **kwargs):
        lc = LandedCost.objects.create(purchase_invoice=self.invoice, allocation=allocation, **kwargs)
        lc.charges.create(description="شحن بحري", amount=200, supplier=self.shipper)
        lc.charges.create(description="رسوم جمركية", amount=100, account=Account.objects.get(number="1102"))
        lc.calculate_totals()
        lc.save()
        return lc

    def test_allocation_by_value_splits_stock_and_cogs(self):
        cogs_before = balance(account=self.company.default_cogs_account)
        stock_before = balance(account=self.company.default_stock_account)
        lc = self.landed()
        lc.submit()
        rows = {r.item.code: r for r in lc.items.all()}
        self.assertEqual((rows["LA"].allocated, rows["LA"].to_stock, rows["LA"].to_cogs),
                         (Decimal("150.00"), Decimal("90.00"), Decimal("60.00")))      # 6 من 10 باقية
        self.assertEqual((rows["LB"].to_stock, rows["LB"].to_cogs), (Decimal("150.00"), Decimal("0.00")))
        self.assertEqual(Bin.objects.get(item=self.a, warehouse=self.wh).valuation_rate, Decimal("115"))
        self.assertEqual(Bin.objects.get(item=self.b, warehouse=self.wh).valuation_rate, Decimal("230"))
        self.assertEqual(balance(account=self.company.default_stock_account) - stock_before, Decimal("240"))
        self.assertEqual(balance(account=self.company.default_cogs_account) - cogs_before, Decimal("60"))
        self.assertEqual(balance(supplier=self.shipper), Decimal("-200"))
        self.assertEqual(balance(), 0)
        lc.cancel()
        self.assertEqual(Bin.objects.get(item=self.a, warehouse=self.wh).valuation_rate, Decimal("100"))
        self.assertEqual(balance(supplier=self.shipper), 0)

    def test_by_qty_and_validation(self):
        lc = self.landed(allocation="qty")
        lc.submit()
        rows = {r.item.code: r.allocated for r in lc.items.all()}
        self.assertEqual(rows, {"LA": Decimal("200.00"), "LB": Decimal("100.00")})    # 10 : 5
        empty = LandedCost.objects.create(purchase_invoice=self.invoice)
        with self.assertRaisesMessage(ValidationError, "تكلفة"):
            empty.submit()
        neither = LandedCost.objects.create()
        neither.charges.create(description="x", amount=1, supplier=self.shipper)
        with self.assertRaises(ValidationError):
            neither.submit()

    def test_pages(self):
        lc = self.landed()
        lc.submit()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(lc.get_absolute_url()), "التوزيع على أصناف الشحنة")
        self.assertEqual(self.client.get("/app/landed-cost/new/").status_code, 200)
