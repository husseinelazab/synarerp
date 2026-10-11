import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from buying.models import PurchaseInvoice, Supplier
from core.models import Company, Membership, Notification, Role
from selling.models import Customer, SalesInvoice
from stock.models import (Batch, BatchBin, Bin, Item, StockCount, StockEntry, StockLedgerEntry, Warehouse)

TODAY = timezone.localdate()


def days(n):
    return TODAY + datetime.timedelta(days=n)


class BatchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        ref = Item.objects.get(code="MOU-001")
        cls.milk = Item.objects.create(company=cls.company, code="MILK", name="حليب", item_group=ref.item_group,
                                       uom=ref.uom, selling_rate=10, buying_rate=6, has_batch_no=True,
                                       shelf_life_days=14)
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.wh2 = Warehouse.objects.create(company=cls.company, name="فرع جدة")
        cls.supplier = Supplier.objects.first()
        cls.customer = Customer.objects.first()

    def setUp(self):
        self.client.force_login(self.owner)

    def buy(self, qty, batch_no="", expiry=None, item=None):
        inv = PurchaseInvoice.objects.create(company=self.company, supplier=self.supplier, warehouse=self.wh)
        inv.items.create(item=item or self.milk, qty=qty, rate=6, batch_no=batch_no, expiry_date=expiry)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def sell(self, qty, item=None, submit=True):
        inv = SalesInvoice.objects.create(company=self.company, customer=self.customer, warehouse=self.wh)
        inv.items.create(item=item or self.milk, qty=qty, rate=10)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
        return inv

    def qty(self, batch_no, warehouse=None):
        bb = BatchBin.objects.filter(batch__batch_no=batch_no, warehouse=warehouse or self.wh).first()
        return bb.qty if bb else Decimal(0)

    def stock(self):
        self.setup_batches = True
        self.buy(10, "OLD", days(-1))     # منتهية
        self.buy(10, "B-SOON", days(5))
        self.buy(10, "B-LATE", days(60))

    def test_fefo_skips_expired_and_splits(self):
        self.stock()
        inv = self.sell(15)
        self.assertEqual((self.qty("OLD"), self.qty("B-SOON"), self.qty("B-LATE")), (10, 0, 5))
        sles = StockLedgerEntry.objects.filter(voucher_type="sales-invoice", voucher_id=inv.pk).order_by("id")
        self.assertEqual([(s.batch.batch_no, s.actual_qty) for s in sles], [("B-SOON", -10), ("B-LATE", -5)])
        # الرصيد الإجمالي والتقييم على مستوى الصنف كما هو
        self.assertEqual(Bin.objects.get(item=self.milk, warehouse=self.wh).actual_qty, 15)
        with self.assertRaises(ValidationError) as ctx:
            self.sell(6)
        self.assertIn("منتهي الصلاحية", str(ctx.exception))
        inv.cancel()
        self.assertEqual((self.qty("B-SOON"), self.qty("B-LATE")), (10, 10))

    def test_sales_return_goes_back_to_sold_batches(self):
        self.stock()
        inv = self.sell(12)
        ret = inv.make_return(self.owner, {self.milk.pk: 12})
        ret.submit()
        self.assertEqual((self.qty("B-SOON"), self.qty("B-LATE")), (10, 10))

    def test_purchase_return_of_expired_batch_and_disposal(self):
        self.stock()
        original = PurchaseInvoice.objects.filter(items__batch_no="OLD").get()
        ret = original.make_return(self.owner, {self.milk.pk: 4})
        ret.submit()
        self.assertEqual(self.qty("OLD"), 6)
        issue = StockEntry.objects.create(company=self.company, purpose="issue", from_warehouse=self.wh)
        issue.items.create(item=self.milk, qty=6, batch_no="OLD")
        issue.calculate_totals()
        issue.save()
        issue.submit()  # الإتلاف مسموح بالتشغيلة المنتهية
        self.assertEqual(self.qty("OLD"), 0)

    def test_auto_batch_with_shelf_life_and_expiry_mismatch(self):
        inv = self.buy(5)
        batch = Batch.objects.get(item=self.milk)
        self.assertEqual(batch.batch_no, f"MILK-{inv.posting_date:%y%m%d}")
        self.assertEqual(batch.expiry_date, inv.posting_date + datetime.timedelta(days=14))
        self.assertEqual(batch.reference, inv.name)
        self.buy(5)
        self.assertEqual(Batch.objects.filter(item=self.milk).count(), 2)  # رقم مختلف لنفس اليوم
        with self.assertRaises(ValidationError):
            self.buy(1, batch.batch_no, days(99))

    def test_transfer_keeps_batches(self):
        self.stock()
        entry = StockEntry.objects.create(company=self.company, purpose="transfer", from_warehouse=self.wh,
                                          to_warehouse=self.wh2)
        entry.items.create(item=self.milk, qty=12)
        entry.calculate_totals()
        entry.save()
        entry.submit()
        self.assertEqual((self.qty("B-SOON", self.wh2), self.qty("B-LATE", self.wh2)), (10, 2))
        self.assertEqual(self.qty("B-LATE"), 8)
        self.assertEqual(Bin.objects.get(item=self.milk, warehouse=self.wh2).stock_value, Decimal("72.00"))
        entry.cancel()
        self.assertEqual(self.qty("B-SOON", self.wh2), 0)

    def test_enabling_tracking_on_existing_stock(self):
        mouse = Item.objects.get(code="MOU-001")
        on_hand = Bin.objects.get(item=mouse, warehouse=self.wh).actual_qty
        mouse.has_batch_no = True
        mouse.save()
        self.assertEqual(self.qty("OPEN-MOU-001"), on_hand)
        self.sell(2, item=mouse)
        self.assertEqual(self.qty("OPEN-MOU-001"), on_hand - 2)
        mouse.save()  # لا يُكرر
        self.assertEqual(self.qty("OPEN-MOU-001"), on_hand - 2)

    def test_stock_count_losses_fefo_and_gains_to_latest(self):
        self.stock()
        count = StockCount.objects.create(company=self.company, warehouse=self.wh)
        count.calculate_totals()
        count.save()
        count.items.filter(item=self.milk).update(counted_qty=18)  # عجز 12
        count.calculate_totals()
        count.save()
        count.submit()
        self.assertEqual((self.qty("OLD"), self.qty("B-SOON"), self.qty("B-LATE")), (0, 8, 10))
        gain = StockCount.objects.create(company=self.company, warehouse=self.wh)
        gain.calculate_totals()
        gain.save()
        gain.items.filter(item=self.milk).update(counted_qty=21)
        gain.calculate_totals()
        gain.save()
        gain.submit()
        self.assertEqual(self.qty("B-LATE"), 13)

    def test_pos_sale_uses_fefo(self):
        from pos.models import POSProfile, POSShift
        from pos.services import checkout

        self.stock()
        cashier = User.objects.create_user("cashier", password="pass12345")
        Membership.objects.create(user=cashier, company=self.company, role=Role.CASHIER)
        profile = POSProfile.objects.get()
        shift = POSShift.open(profile, cashier, opening_cash=0)
        checkout(shift, cashier, [{"item": self.milk.pk, "qty": 3}], paid_cash=100)
        self.assertEqual(self.qty("B-SOON"), 7)

    def test_report_and_daily_alert(self):
        self.stock()
        page = self.client.get("/reports/stock/batches/")
        self.assertEqual(page.context["counts"]["expired"], 1)
        self.assertEqual(page.context["counts"]["near"], 1)
        self.assertContains(page, "B-SOON")
        self.assertEqual(len(self.client.get("/reports/stock/batches/", {"status": "expired"}).context["rows"]), 1)
        call_command("expiry_alerts", stdout=open("/dev/null", "w"))
        call_command("expiry_alerts", stdout=open("/dev/null", "w"))
        titles = list(Notification.objects.filter(user=self.owner).values_list("title", flat=True))
        self.assertEqual(len([t for t in titles if "منتهية الصلاحية" in t]), 1)
        self.assertTrue(any("تنتهي خلال" in t and "B-SOON" in t for t in titles))
        self.assertContains(self.client.get(f"/app/batch/{Batch.objects.get(batch_no='OLD').pk}/"), "OLD")
