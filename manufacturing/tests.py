import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from buying.models import PurchaseInvoice, Supplier
from core.models import Company, Membership, Role
from manufacturing.models import BOM, OVERHEAD_ACCOUNT, Production
from stock.models import Batch, BatchBin, Bin, Item, Warehouse

TODAY = timezone.localdate()


class ManufacturingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        ref = Item.objects.get(code="MOU-001")
        make = lambda code, name, **kw: Item.objects.create(  # noqa: E731
            company=cls.company, code=code, name=name, item_group=ref.item_group, uom=ref.uom, **kw)
        cls.flour = make("FLOUR", "دقيق", buying_rate=6, has_batch_no=True)
        cls.sugar = make("SUGAR", "سكر", buying_rate=4)
        cls.cake = make("CAKE", "كيك", selling_rate=20, has_batch_no=True, shelf_life_days=5)
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.supplier = Supplier.objects.first()

    def setUp(self):
        self.client.force_login(self.owner)
        self.buy(self.flour, 10, 6, "F1", TODAY + datetime.timedelta(days=10))
        self.buy(self.flour, 10, 6, "F2", TODAY + datetime.timedelta(days=40))
        self.buy(self.sugar, 10, 4)
        self.bom = BOM.objects.create(company=self.company, item=self.cake, quantity=10, operating_cost=15)
        self.bom.items.create(item=self.flour, qty=2)
        self.bom.items.create(item=self.sugar, qty=1)

    def buy(self, item, qty, rate, batch_no="", expiry=None):
        inv = PurchaseInvoice.objects.create(company=self.company, supplier=self.supplier, warehouse=self.wh)
        inv.items.create(item=item, qty=qty, rate=rate, batch_no=batch_no, expiry_date=expiry)
        inv.calculate_totals()
        inv.save()
        inv.submit()

    def production(self, qty=20, **kw):
        p = Production.objects.create(company=self.company, bom=self.bom, qty=qty, source_warehouse=self.wh,
                                      target_warehouse=self.wh, **kw)
        p.calculate_totals()
        p.save()
        return p

    def gl_net(self, account):
        agg = GLEntry.objects.filter(account=account, is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or 0) - (agg["c"] or 0)

    def test_bom_costing_and_producible(self):
        cost = self.bom.cost_breakdown()
        self.assertEqual((cost["material"], cost["total"], cost["unit"]), (Decimal("16"), Decimal("31"), Decimal("3.10")))
        self.assertEqual(self.bom.producible(self.wh)[0], Decimal("100"))
        Batch.objects.filter(batch_no="F1").update(expiry_date=TODAY - datetime.timedelta(days=1))
        self.assertEqual(self.bom.producible(self.wh), (Decimal("50"), self.flour))  # الدقيق الصالح 10 فقط
        page = self.client.get(f"/app/bom/{self.bom.pk}/")
        self.assertContains(page, "الكمية الممكن إنتاجها")
        self.assertContains(page, f"?bom={self.bom.pk}&qty=10")
        # قائمة افتراضية واحدة لكل منتج
        other = BOM.objects.create(company=self.company, item=self.cake, quantity=1)
        self.bom.refresh_from_db()
        self.assertFalse(self.bom.is_default)
        self.assertTrue(other.is_default)

    def test_production_flow(self):
        p = self.production(20)
        rows = {r.item.code: r for r in p.items.select_related("item")}
        self.assertEqual((rows["FLOUR"].qty, rows["SUGAR"].qty), (Decimal("4"), Decimal("2")))
        self.assertEqual(p.operating_cost, Decimal("30"))
        self.assertEqual(p.item, self.cake)
        p.submit()
        p.refresh_from_db()
        # المواد بتكلفتها الفعلية + تكاليف التشغيل
        self.assertEqual((p.material_cost, p.total_cost, p.unit_cost), (Decimal("32"), Decimal("62"), Decimal("3.1")))
        self.assertEqual(BatchBin.objects.get(batch__batch_no="F1").qty, 6)  # الأقرب انتهاءً أولاً
        cake_bin = Bin.objects.get(item=self.cake, warehouse=self.wh)
        self.assertEqual((cake_bin.actual_qty, cake_bin.stock_value), (Decimal("20"), Decimal("62.00")))
        batch = p.output_batch
        self.assertEqual(batch.expiry_date, p.posting_date + datetime.timedelta(days=5))
        from accounts.models import Account

        overhead = Account.objects.get(name=OVERHEAD_ACCOUNT)
        self.assertEqual(self.gl_net(overhead), Decimal("-30"))
        stock = self.company.require_account("default_stock_account")
        self.assertEqual(GLEntry.objects.filter(voucher_type="production", voucher_id=p.pk).aggregate(
            d=Sum("debit"))["d"], Decimal("30"))
        self.assertContains(self.client.get(f"/app/production/{p.pk}/"), batch.batch_no)
        p.cancel()
        self.assertEqual(BatchBin.objects.get(batch__batch_no="F1").qty, 10)
        self.assertEqual(Bin.objects.get(item=self.cake, warehouse=self.wh).actual_qty, 0)
        self.assertEqual(self.gl_net(overhead), 0)
        self.assertIsNotNone(stock)

    def test_shortage_blocks_and_expired_material_not_used(self):
        p = self.production(200)  # يحتاج 40 دقيق و20 سكر
        self.assertTrue(p.has_shortage)
        self.assertContains(self.client.get(f"/app/production/{p.pk}/"), "لا يكفي رصيد")
        with self.assertRaises(ValidationError):
            p.submit()
        Batch.objects.filter(batch_no="F1").update(expiry_date=TODAY - datetime.timedelta(days=1))
        p2 = self.production(60)  # 12 دقيق: الصالح 10 فقط
        with self.assertRaises(ValidationError) as ctx:
            p2.submit()
        self.assertIn("منتهي الصلاحية", str(ctx.exception))

    def test_manual_consumption_and_batch_override(self):
        p = self.production(10, manual_consumption=True, batch_no="CAKE-A", additional_cost=5)
        p.items.filter(item=self.flour).update(qty=3, batch_no="F2")
        p.calculate_totals()
        p.save()
        self.assertEqual(p.items.get(item=self.flour).qty, 3)  # لم يُعَد حسابها
        p.submit()
        p.refresh_from_db()
        self.assertEqual(BatchBin.objects.get(batch__batch_no="F2").qty, 7)
        self.assertEqual(p.total_cost, Decimal("3") * 6 + 4 + 15 + 5)
        self.assertEqual(p.output_batch.batch_no, "CAKE-A")

    def test_new_production_form_from_bom_link(self):
        page = self.client.get("/app/production/new/", {"bom": self.bom.pk, "qty": "10",
                                                        "source_warehouse": self.wh.pk, "target_warehouse": self.wh.pk})
        self.assertEqual(page.status_code, 200)
        response = self.client.post("/app/production/new/", {
            "bom": self.bom.pk, "qty": "10", "posting_date": TODAY.isoformat(), "source_warehouse": self.wh.pk,
            "target_warehouse": self.wh.pk, "batch_no": "", "expiry_date": "", "additional_cost": "0", "remarks": "",
            "items-TOTAL_FORMS": "0", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 302, response.content.decode()[:2000])
        p = Production.objects.latest("id")
        self.assertEqual(p.items.count(), 2)
        self.assertEqual(p.total_cost, Decimal("31"))

    def test_module_permission(self):
        maker = User.objects.create_user("maker", password="pass12345")
        Membership.objects.create(user=maker, company=self.company, role=Role.USER, modules=["manufacturing"])
        self.client.force_login(maker)
        self.assertEqual(self.client.get("/app/production/").status_code, 200)
        self.assertEqual(self.client.get("/reports/stock/batches/").status_code, 200)
        self.assertNotEqual(self.client.get("/app/sales-invoice/").status_code, 200)
