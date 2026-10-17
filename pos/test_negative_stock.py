from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import GLEntry
from buying.models import PurchaseInvoice, Supplier
from core.models import Membership, Role
from core.tenancy import tenant_context
from pos.models import POSProfile, POSShift
from pos.services import checkout, pos_return
from saas.tests import make_company
from selling.models import SalesInvoice
from stock.models import Bin, Item, ItemGroup, UOM, NegativeStockSettlement, StockLedgerEntry
from stock.profit import _stock_costs


class NegativeStockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("متجر البيع بالسالب", "negative_owner")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.profile = POSProfile.objects.get()
        self.profile.allow_negative_stock = True
        self.profile.save()
        self.shift = POSShift.open(self.profile, self.user, 1000)
        self.item = Item.objects.create(code="NEG", name="صنف بسالب", item_group=ItemGroup.objects.first(),
            uom=UOM.objects.first(), buying_rate=20, selling_rate=100)
        self.supplier = Supplier.objects.create(name="مورد")

    def sell(self, qty):
        return checkout(self.shift, self.user, [{"item": self.item.pk, "qty": qty}], paid_cash=2000)

    def purchase(self, qty, rate):
        invoice = PurchaseInvoice.objects.create(supplier=self.supplier, warehouse=self.profile.warehouse, update_stock=True)
        invoice.items.create(item=self.item, qty=qty, rate=rate)
        invoice.submit()
        return invoice

    def assert_balance(self, qty, value):
        b = Bin.objects.get(item=self.item, warehouse=self.profile.warehouse)
        self.assertEqual(b.actual_qty, Decimal(str(qty)))
        self.assertEqual(b.stock_value, Decimal(str(value)))
        stock = GLEntry.objects.filter(account=self.company.default_stock_account, is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual((stock["d"] or 0)-(stock["c"] or 0), b.stock_value)
        gl = GLEntry.objects.filter(is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(gl["d"], gl["c"])
        pending = StockLedgerEntry.objects.filter(item=self.item, warehouse=self.profile.warehouse, is_cancelled=False).aggregate(q=Sum("negative_remaining_qty"), v=Sum("negative_remaining_value"))
        self.assertEqual(pending["q"] or 0, max(-b.actual_qty, 0))
        if b.actual_qty < 0:
            self.assertEqual(pending["v"], -b.stock_value)
        return b

    def test_switch_off_by_default_and_missing_cost_are_blocked(self):
        self.assertFalse(POSProfile._meta.get_field("allow_negative_stock").default)
        POSProfile.objects.filter(pk=self.profile.pk).update(allow_negative_stock=False)
        with self.assertRaises(ValidationError):
            self.sell(1)
        POSProfile.objects.filter(pk=self.profile.pk).update(allow_negative_stock=True)
        self.item.buying_rate = 0
        self.item.save()
        with self.assertRaises(ValidationError):
            self.sell(1)
        self.assertFalse(SalesInvoice.objects.filter(is_pos=True).exists())

    def test_issue_receipts_cost_reconciliation_and_cancellation(self):
        sale = self.sell(2)
        self.assert_balance(-2, -40)
        first = self.purchase(1, 30)
        self.assert_balance(-1, -20)
        self.assertEqual(_stock_costs("sales-invoice", [sale.pk])[(sale.pk,self.item.pk)][0], 50)
        second = self.purchase(2, 40)
        self.assert_balance(1, 40)
        self.assertEqual(_stock_costs("sales-invoice", [sale.pk])[(sale.pk,self.item.pk)][0], 70)
        with self.assertRaises(ValidationError):
            sale.cancel()
        with self.assertRaises(ValidationError):
            first.cancel()
        first.refresh_from_db()
        sale.refresh_from_db()
        second.cancel()
        self.assert_balance(-1, -20)
        first.cancel()
        self.assert_balance(-2, -40)
        sale.cancel()
        self.assert_balance(0, 0)

    def test_fifo_deficit_and_rebuild(self):
        self.item.valuation_method = "fifo"
        self.item.save()
        self.purchase(1, 10)
        sale = self.sell(3)
        self.assert_balance(-2, -20)
        receipt = self.purchase(3, 30)
        queue = self.assert_balance(1,30).fifo_queue
        self.assertEqual([(Decimal(q),Decimal(r)) for q,r in queue], [(Decimal(1),Decimal(30))])
        receipt.cancel()
        self.assert_balance(-2,-20)
        sale.cancel()
        self.assert_balance(1,10)

    def test_return_covers_deficit_and_can_be_cancelled(self):
        sale = self.sell(2)
        self.purchase(1,30)
        ret = pos_return(self.shift,self.user,sale.name,[{"item":self.item.pk,"qty":1}])
        self.assert_balance(0,0)
        ret.cancel()
        self.assert_balance(-1,-20)

    def test_tracking_and_ordinary_sales_cannot_bypass(self):
        for name in ("has_serial_no", "has_batch_no"):
            Item.objects.filter(pk=self.item.pk).update(**{name:True})
            with self.assertRaises(ValidationError):
                self.sell(1)
            Item.objects.filter(pk=self.item.pk).update(**{name:False})
        invoice = SalesInvoice.objects.create(customer=self.profile.default_customer, warehouse=self.profile.warehouse,update_stock=True)
        invoice.items.create(item=self.item,qty=1,rate=100)
        with self.assertRaises(ValidationError):
            invoice.submit()

    def test_only_managers_can_change_profile_and_report_filters(self):
        Membership.objects.filter(user=self.user,company=self.company).update(role=Role.USER)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(f"/app/pos-profile/{self.profile.pk}/edit/",{"allow_negative_stock":"on"}).status_code,302)
        Membership.objects.filter(user=self.user,company=self.company).update(role=Role.OWNER)
        self.sell(1)
        response = self.client.get("/reports/stock/balance/?negative=1")
        self.assertContains(response,"متابعة الأرصدة السالبة")
        self.assertEqual(len(response.context["bins"]),1)

    def test_quote_warnings_and_return_profit_match_posted_cogs(self):
        from pos.services import quote
        result=quote(self.shift,[{"item":self.item.pk,"qty":2}])
        self.assertEqual(result["stock_warnings"],[{"name":self.item.name,"available":0.0,"after":-2.0,"allowed":True}])
        sale=self.sell(2)
        self.purchase(1,30)
        ret=pos_return(self.shift,self.user,sale.name,[{"item":self.item.pk,"qty":1}])
        costs=_stock_costs("sales-invoice",[sale.pk,ret.pk])
        net=costs[(sale.pk,self.item.pk)][0]+costs[(ret.pk,self.item.pk)][0]
        gl=GLEntry.objects.filter(account=self.company.default_cogs_account,is_cancelled=False).aggregate(d=Sum("debit"),c=Sum("credit"))
        self.assertEqual(net,(gl["d"] or 0)-(gl["c"] or 0))
        self.assert_balance(0,0)
