from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from buying.models import PurchaseOrder, Supplier
from core.models import Company, Membership, Notification, Role
from selling.models import Customer, SalesInvoice
from stock.models import Bin, Item, Warehouse
from stock.reorder import reorder_rows


class ReorderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.mouse = Item.objects.get(code="MOU-001")
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.supplier = Supplier.objects.first()

    def setUp(self):
        self.client.force_login(self.owner)
        self.on_hand = Bin.objects.get(item=self.mouse, warehouse=self.wh).actual_qty
        Item.objects.filter(pk=self.mouse.pk).update(reorder_level=self.on_hand + 10, reorder_qty=100)

    def test_below_level_suggestion_and_open_po(self):
        rows = {r["item"].code: r for r in reorder_rows()}
        row = rows["MOU-001"]
        self.assertTrue(row["below"])
        self.assertEqual(row["suggested"], Decimal("100"))
        self.assertEqual(row["supplier"], self.supplier)  # آخر مورد من فاتورة الشراء التجريبية
        self.assertNotIn("LAP-001", rows)  # بلا حد
        po = PurchaseOrder.objects.create(company=self.company, supplier=self.supplier, warehouse=self.wh)
        po.items.create(item=self.mouse, qty=20, rate=40)
        po.calculate_totals()
        po.save()
        po.submit()
        self.assertNotIn("MOU-001", {r["item"].code for r in reorder_rows()})  # المطلوب يغطي الحد

    def test_velocity_and_cover(self):
        from stock.reorder import daily_sales

        before = daily_sales().get(self.mouse.pk, Decimal(0)) * 30
        inv = SalesInvoice.objects.create(company=self.company, customer=Customer.objects.first(), warehouse=self.wh)
        inv.items.create(item=self.mouse, qty=30, rate=75)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        row = next(r for r in reorder_rows() if r["item"] == self.mouse)
        per_day = ((before + 30) / 30).quantize(Decimal("0.001"))
        self.assertEqual(row["per_day"], per_day)
        self.assertEqual(row["cover_days"], int((self.on_hand - 30) / per_day))
        self.assertEqual(row["suggested_level"], (per_day * 7).quantize(Decimal("0.001")))

    def test_page_creates_purchase_orders_and_alert(self):
        page = self.client.get("/reports/stock/reorder/")
        self.assertContains(page, "فأرة لاسلكية")
        self.assertEqual(page.context["count"], 1)
        self.assertGreater(len(self.client.get("/reports/stock/reorder/", {"all": "1"}).context["rows"]), 1)
        response = self.client.post("/reports/stock/reorder/", {
            "pick": [str(self.mouse.pk)], f"qty_{self.mouse.pk}": "120", f"supplier_{self.mouse.pk}": self.supplier.pk})
        self.assertEqual(response.status_code, 302)
        po = PurchaseOrder.objects.get()
        self.assertEqual((po.supplier, po.items.get().qty, po.docstatus), (self.supplier, Decimal("120"), 0))
        call_command("reorder_alerts", stdout=open("/dev/null", "w"))
        call_command("reorder_alerts", stdout=open("/dev/null", "w"))
        self.assertEqual(Notification.objects.filter(user=self.owner, title__contains="حد إعادة الطلب").count(), 1)
