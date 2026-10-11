import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from buying.models import PurchaseInvoice, Supplier
from core.importer import run_import
from core.models import Company, Membership, Notification, Role
from core.registry import get_doctype
from core.tenancy import set_current_user
from selling.models import Customer, SalesInvoice
from stock.models import Item, ItemPriceHistory, Warehouse
from stock.profit import gross_profit


class PricingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.laptop = Item.objects.get(code="LAP-001")
        cls.mouse = Item.objects.get(code="MOU-001")
        cls.supplier = Supplier.objects.first()
        cls.customer = Customer.objects.first()
        cls.warehouse = Warehouse.objects.first()

    def setUp(self):
        self.client.force_login(self.owner)
        from pos.models import POSProfile

        POSProfile.objects.update(prices_include_tax=False)

    def purchase(self, item, qty, rate, submit=True):
        inv = PurchaseInvoice.objects.create(company=self.company, supplier=self.supplier, warehouse=self.warehouse)
        inv.items.create(item=item, qty=qty, rate=rate)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
        return inv

    def sell(self, items, discount=0, update_stock=True):
        inv = SalesInvoice.objects.create(company=self.company, customer=self.customer, warehouse=self.warehouse,
                                          discount_amount=discount, update_stock=update_stock)
        for item, qty, rate in items:
            inv.items.create(item=item, qty=qty, rate=rate)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    # ---------- استيراد الأسعار ----------
    def test_import_update_with_code_and_price_only(self):
        content = "كود الصنف,سعر البيع\nLAP-001,3500\nMOU-001,80.5\nNEW-1,10\n".encode()
        result = run_import(get_doctype("item"), "prices.csv", content, mode="update", valid_only=True, dry_run=False,
                            user=self.owner)
        self.assertEqual((result["updated"], result["failed"]), (2, 1), result["errors"])
        self.laptop.refresh_from_db()
        self.assertEqual((self.laptop.selling_rate, self.laptop.name), (Decimal("3500"), "حاسب محمول"))
        history = ItemPriceHistory.objects.get(item=self.laptop)
        self.assertEqual((history.field, history.old_rate, history.new_rate, history.source, history.reference),
                         ("selling_rate", Decimal("3200"), Decimal("3500"), "import", "prices.csv"))
        # الإضافة الجديدة ما زالت تتطلب كل الأعمدة الإلزامية
        insert = run_import(get_doctype("item"), "p.csv", content, mode="insert", dry_run=True)
        self.assertTrue(insert["missing_columns"])

    def test_manual_change_recorded_with_user(self):
        token = set_current_user(self.owner)
        try:
            self.mouse.selling_rate = 90
            self.mouse.save()
            self.mouse.name = "فأرة"
            self.mouse.save()  # تغيير بدون سعر لا يُسجَّل
        finally:
            from core.tenancy import _current_user

            _current_user.reset(token)
        entries = list(ItemPriceHistory.objects.filter(item=self.mouse))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].user, entries[0].source, entries[0].change_percent), (self.owner, "manual", Decimal("20")))

    # ---------- سعر الشراء من آخر شراء ----------
    def test_purchase_updates_buying_rate_and_cancel_restores(self):
        first = self.purchase(self.laptop, 2, 2600)
        self.laptop.refresh_from_db()
        self.assertEqual(self.laptop.buying_rate, Decimal("2600"))
        second = self.purchase(self.laptop, 1, 2400)
        self.laptop.refresh_from_db()
        self.assertEqual(self.laptop.buying_rate, Decimal("2400"))
        history = ItemPriceHistory.objects.filter(item=self.laptop, field="buying_rate").first()
        self.assertEqual((history.source, history.reference), ("purchase", second.name))
        second.cancel()
        self.laptop.refresh_from_db()
        self.assertEqual(self.laptop.buying_rate, Decimal("2600"))
        self.assertTrue(first.is_submitted)

    def test_return_and_disabled_setting_do_not_update(self):
        inv = self.purchase(self.mouse, 10, 45)
        self.mouse.refresh_from_db()
        self.assertEqual(self.mouse.buying_rate, Decimal("45"))
        ret = inv.make_return(self.owner)
        ret.submit()
        self.mouse.refresh_from_db()
        self.assertEqual(self.mouse.buying_rate, Decimal("45"))
        Company.objects.filter(pk=self.company.pk).update(update_buying_rate_on_purchase=False)
        self.company.refresh_from_db()
        self.purchase(self.mouse, 1, 50)
        self.mouse.refresh_from_db()
        self.assertEqual(self.mouse.buying_rate, Decimal("45"))

    def test_alert_when_cost_exceeds_selling_price(self):
        self.purchase(self.mouse, 5, 76)  # البيع 75
        note = Notification.objects.filter(title__contains="أعلى من سعر البيع").first()
        self.assertIsNotNone(note)
        self.assertIn("فأرة", note.title)
        self.assertIn("status=loss", note.url)

    # ---------- تقرير الربحية ----------
    def test_gross_profit_matches_ledger(self):
        today = timezone.localdate()
        start = today - datetime.timedelta(days=365)
        before_rows, before = gross_profit(start, today)
        inv = self.sell([(self.laptop, 2, 3200), (self.mouse, 4, 75)], discount=100)
        rows, totals = gross_profit(start, today)
        self.assertEqual(totals["sales"] - before["sales"], inv.base_net_total)
        cogs = self.company.require_account("default_cogs_account")
        gl = GLEntry.objects.filter(account=cogs, is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(totals["cost"], (gl["d"] or 0) - (gl["c"] or 0))
        # المرتجع يُخصم من المبيعات والتكلفة
        ret = inv.make_return(self.owner, {self.mouse.pk: 4})
        ret.submit()
        _, after = gross_profit(start, today)
        self.assertEqual(after["sales"], totals["sales"] + ret.base_net_total)
        gl = GLEntry.objects.filter(account=cogs, is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(after["cost"], (gl["d"] or 0) - (gl["c"] or 0))
        by_invoice, _ = gross_profit(start, today, "invoice")
        self.assertIn(inv.pk, [r["key"] for r in by_invoice])

    def test_gross_profit_page_groupings_and_csv(self):
        self.sell([(self.mouse, 40, 30)])  # بيع بأقل من التكلفة 40
        for group in ("item", "item_group", "customer", "invoice", "cost_center", "month"):
            self.assertEqual(self.client.get("/reports/stock/gross-profit/", {"group_by": group}).status_code, 200)
        page = self.client.get("/reports/stock/gross-profit/", {"sort": "loss"})
        self.assertContains(page, "فأرة لاسلكية")
        self.assertContains(page, "row-loss")
        csv_text = self.client.get("/reports/stock/gross-profit/", {"format": "csv"}).content.decode("utf-8-sig")
        self.assertIn("مجمل الربح", csv_text)

    # ---------- تحليل التسعير ----------
    def test_pricing_analysis_statuses_and_suggestion(self):
        Item.objects.filter(pk=self.mouse.pk).update(buying_rate=80)      # بيع 75 → خسارة
        Item.objects.filter(pk=self.laptop.pk).update(buying_rate=3000)   # هامش 6% → منخفض
        page = self.client.get("/reports/stock/pricing/", {"target": 20})
        rows = {r["item"].code: r for r in page.context["rows"]}
        self.assertEqual(rows["MOU-001"]["status"], "loss")
        self.assertEqual(rows["LAP-001"]["status"], "low")
        self.assertEqual(rows["MOU-001"]["suggested"], Decimal("100.00"))  # 80 ÷ (1 − 20%)
        self.assertEqual(rows["PRN-001"]["status"], "ok")
        self.assertEqual(page.context["rows"][0]["item"].code, "MOU-001")  # الخاسر أولاً
        # مع أسعار شاملة الضريبة: السعر المقترح يُعاد شاملاً
        from pos.models import POSProfile

        POSProfile.objects.update(prices_include_tax=True)
        rows = {r["item"].code: r for r in self.client.get("/reports/stock/pricing/", {"target": 20}).context["rows"]}
        self.assertEqual(rows["MOU-001"]["suggested"], Decimal("115.00"))

    def test_export_suggested_prices_then_reimport(self):
        Item.objects.filter(pk=self.mouse.pk).update(buying_rate=80)
        content = self.client.get("/reports/stock/pricing/", {"status": "loss", "target": 20, "format": "csv"}).content
        result = run_import(get_doctype("item"), "pricing.csv", content, mode="update", dry_run=False, user=self.owner)
        self.assertEqual(result["updated"], 1, result["errors"])
        self.mouse.refresh_from_db()
        self.assertEqual(self.mouse.selling_rate, Decimal("100.00"))

    def test_price_history_page_and_permissions(self):
        self.purchase(self.mouse, 1, 41)
        self.assertContains(self.client.get("/reports/stock/price-history/"), "آخر سعر شراء")
        seller = User.objects.create_user("seller", password="pass12345")
        Membership.objects.create(user=seller, company=self.company, role=Role.USER, modules=["selling"])
        self.client.force_login(seller)
        self.assertEqual(self.client.get("/reports/stock/gross-profit/").status_code, 200)
        self.assertNotEqual(self.client.get("/reports/stock/pricing/").status_code, 200)
