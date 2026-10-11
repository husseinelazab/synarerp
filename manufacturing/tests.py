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
            "rejected_qty": "0", "scrap_qty": "0",
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


class ManufacturingAdvancedTests(ManufacturingTests):
    """المسارات ومحطات العمل، والهالك والمخلفات، والتعدد في المستويات، وتخطيط الاحتياجات."""

    def make_routing(self):
        from manufacturing.models import Routing, Workstation

        oven = Workstation.objects.create(company=self.company, name="الفرن", hourly_rate=60)
        packing = Workstation.objects.create(company=self.company, name="التعبئة", hourly_rate=30)
        routing = Routing.objects.create(company=self.company, name="خبز الكيك")
        routing.operations.create(operation="خبز", workstation=oven, minutes=2)      # 2 ريال للوحدة
        routing.operations.create(operation="تعبئة", workstation=packing, minutes=1)  # 0.5 ريال للوحدة
        return routing

    def test_routing_cost_added(self):
        routing = self.make_routing()
        self.assertEqual(routing.cost_per_unit, Decimal("2.50"))
        self.bom.routing = routing
        self.bom.save()
        self.assertEqual(self.bom.cost_breakdown()["operating"], Decimal("40"))  # 15 + 2.5×10
        p = self.production(20)
        self.assertEqual(p.operating_cost, Decimal("80"))
        p.submit()
        page = self.client.get("/manufacturing/report/")
        self.assertContains(page, "تحميل محطات العمل")
        stations = {s["ws"].name: s for s in page.context["stations"]}
        self.assertEqual((stations["الفرن"]["hours"], stations["الفرن"]["cost"]), (Decimal("0.67"), Decimal("40.00")))

    def test_scrap_percent_rejected_and_scrap_item(self):
        bran = Item.objects.create(company=self.company, code="BRAN", name="نخالة", item_group=self.sugar.item_group,
                                   uom=self.sugar.uom)
        self.bom.items.filter(item=self.flour).update(scrap_percent=10)
        self.bom.scrap_item, self.bom.scrap_qty, self.bom.scrap_rate = bran, 1, 2
        self.bom.save()
        p = self.production(20, rejected_qty=2)
        self.assertEqual(p.items.get(item=self.flour).qty, Decimal("4.4"))
        self.assertEqual((p.scrap_qty, p.scrap_value), (Decimal("2"), Decimal("4")))
        p.submit()
        p.refresh_from_db()
        # المواد 4.4×6 + 2×4 = 34.4، التشغيل 30، يُخصم 4 مخلفات = 60.4 على 18 وحدة سليمة
        self.assertEqual((p.material_cost, p.total_cost), (Decimal("34.40"), Decimal("60.40")))
        self.assertEqual(p.unit_cost, Decimal("3.3556"))
        self.assertEqual(Bin.objects.get(item=self.cake, warehouse=self.wh).actual_qty, 18)
        self.assertEqual(Bin.objects.get(item=bran, warehouse=self.wh).stock_value, Decimal("4.00"))
        summary = self.client.get("/manufacturing/report/").context["summary"][0]
        self.assertEqual((summary["good"], summary["waste"]), (Decimal("18"), Decimal("10")))
        with self.assertRaises(ValidationError):
            self.production(5, rejected_qty=5).submit()

    def test_multilevel_bom_cost_and_sub_productions(self):
        # تغليف الكيك: كيك + علبة. الكيك نصف مصنّع له قائمة مواد
        box = Item.objects.create(company=self.company, code="BOX", name="علبة", item_group=self.sugar.item_group,
                                  uom=self.sugar.uom, buying_rate=1)
        gift = Item.objects.create(company=self.company, code="GIFT", name="علبة هدايا كيك",
                                   item_group=self.sugar.item_group, uom=self.sugar.uom, selling_rate=50)
        self.buy(box, 10, 1)
        top = BOM.objects.create(company=self.company, item=gift, quantity=1, operating_cost=0)
        top.items.create(item=self.cake, qty=4)
        top.items.create(item=box, qty=1)
        self.assertTrue(top.is_multilevel)
        self.assertEqual([r["level"] for r in top.tree], [0, 1, 1, 0])
        # لا رصيد كيك: تكلفته من قائمته (3.10)
        self.assertEqual(top.cost_breakdown()["unit"], Decimal("13.40"))
        p = Production.objects.create(company=self.company, bom=top, qty=5, source_warehouse=self.wh,
                                      target_warehouse=self.wh)
        p.calculate_totals()
        p.save()
        self.assertEqual(p.draft_actions, [("make_sub_productions", "إنشاء أوامر إنتاج للمكونات الناقصة")])
        response = self.client.post(f"/app/production/{p.pk}/make_sub_productions/")
        sub = Production.objects.exclude(pk=p.pk).get(item=self.cake)
        self.assertRedirects(response, f"/app/production/{sub.pk}/edit/", fetch_redirect_response=False)
        self.assertEqual(sub.qty, 20)
        sub.submit()
        p.calculate_totals()
        p.save()
        self.assertFalse(p.has_shortage)
        p.submit()
        self.assertEqual(Bin.objects.get(item=gift, warehouse=self.wh).actual_qty, 5)

    def test_mrp_from_sales_orders_and_drafts(self):
        from buying.models import PurchaseOrder
        from selling.models import Customer, SalesOrder

        self.bom.items.create(item=Item.objects.get(code="LAP-001"), qty=0)  # مادة بلا كمية لا تؤثر
        so = SalesOrder.objects.create(company=self.company, customer=Customer.objects.first(), warehouse=self.wh)
        so.items.create(item=self.cake, qty=150, rate=20)
        so.calculate_totals()
        so.save()
        so.submit()
        rows = {r["item"].code: r for r in self.client.get("/manufacturing/mrp/").context["rows"]}
        self.assertEqual((rows["CAKE"]["action"], rows["CAKE"]["shortage"]), ("produce", Decimal("150")))
        # 150 كيكة: دقيق 30 (المتاح 20) وسكر 15 (المتاح 10)
        self.assertEqual((rows["FLOUR"]["shortage"], rows["SUGAR"]["shortage"]), (Decimal("10"), Decimal("5")))
        self.assertEqual(rows["FLOUR"]["supplier"], self.supplier)
        response = self.client.post("/manufacturing/mrp/", {
            "action": "purchase", "productions": "1", "sales_orders": "1",
            "pick": [str(self.flour.pk), str(self.sugar.pk)],
            f"supplier_{self.flour.pk}": self.supplier.pk, f"supplier_{self.sugar.pk}": self.supplier.pk,
            f"qty_{self.flour.pk}": "10", f"qty_{self.sugar.pk}": "5"})
        self.assertEqual(response.status_code, 302)
        po = PurchaseOrder.objects.get()
        self.assertEqual(sorted((r.item.code, r.qty) for r in po.items.all()),
                         [("FLOUR", Decimal("10")), ("SUGAR", Decimal("5"))])
        po.submit()
        rows = {r["item"].code: r for r in self.client.get("/manufacturing/mrp/").context["rows"]}
        self.assertEqual((rows["FLOUR"]["on_order"], rows["FLOUR"]["action"]), (Decimal("10"), "ok"))
        self.client.post("/manufacturing/mrp/", {"action": "production", "productions": "1", "sales_orders": "1",
                                                 "pick": [str(self.cake.pk)], f"qty_{self.cake.pk}": "150"})
        draft = Production.objects.get(item=self.cake, docstatus=0)
        self.assertEqual(draft.qty, 150)
        # المسودة الآن تغطي أمر البيع فلا يُطلب إنتاجه مرة أخرى
        rows = {r["item"].code: r for r in self.client.get("/manufacturing/mrp/").context["rows"]}
        self.assertNotEqual(rows.get("CAKE", {}).get("action"), "produce")
