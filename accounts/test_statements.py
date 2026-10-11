import datetime
from decimal import Decimal

from django.test import TestCase

from accounts.models import Account, JournalEntry, PaymentEntry
from accounts.statements import cash_flow_data
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, OpeningStock, Warehouse


class StatementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة القوائم", "statements")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        self.start = self.today.replace(month=1, day=1)

    def acc(self, number):
        return Account.objects.get(number=number)

    def je(self, debit, credit, amount, date=None):
        je = JournalEntry.objects.create(posting_date=date or self.today)
        je.accounts.create(account=self.acc(debit), debit=amount)
        je.accounts.create(account=self.acc(credit), credit=amount)
        je.submit()

    def test_cash_flow_categories_and_reconciliation(self):
        self.je("1101", "3101", 100000)          # رأس مال نقدي: تمويلي
        self.je("1102", "2201", 50000)           # قرض بنكي: تمويلي
        self.je("1202", "1102", 20000)           # شراء معدات: استثماري
        self.je("5202", "1101", 3000)            # إيجار: تشغيلي
        item = Item.objects.create(code="CF", name="صنف", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                   selling_rate=100)
        stock = OpeningStock.objects.create(warehouse=Warehouse.objects.first())
        stock.items.create(item=item, qty=10, rate=60)
        stock.submit()
        customer = Customer.objects.create(name="عميل")
        inv = SalesInvoice.objects.create(customer=customer, warehouse=Warehouse.objects.first())
        inv.items.create(item=item, qty=2, rate=100)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        PaymentEntry.objects.create(payment_type="receive", customer=customer, amount=230, sales_invoice=inv,
                                    account=self.company.default_cash_account).submit()
        data = cash_flow_data(self.start, self.today)
        totals = {s["key"]: s["total"] for s in data["sections"]}
        self.assertEqual(totals["financing"], Decimal("150000"))
        self.assertEqual(totals["investing"], Decimal("-20000"))
        self.assertEqual(totals["operating"], Decimal("-2770"))  # 230 تحصيل - 3000 إيجار
        self.assertEqual(data["opening"], 0)
        self.assertEqual(data["closing"], Decimal("127230"))
        self.assertTrue(data["reconciles"])
        labels = [r["label"] for r in data["sections"][0]["rows"]]
        self.assertIn("المتحصل من العملاء (صافي)", labels)

    def test_comparative_pages(self):
        last_year = self.today.replace(year=self.today.year - 1)
        from core.models import FiscalYear

        FiscalYear.objects.create(name="السابقة", start_date=last_year.replace(month=1, day=1),
                                  end_date=last_year.replace(month=12, day=31))
        self.je("1101", "4101", 1000, date=last_year)
        self.je("1101", "4101", 1500)
        self.client.force_login(self.user)
        page = self.client.get(f"/reports/accounts/profit-and-loss/compare/?mode=previous_year"
                               f"&from_date={self.start}&to_date={self.today}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["result"], [Decimal("1500"), Decimal("1000")])
        self.assertEqual(page.context["result_variance"]["percent"], Decimal("50"))
        monthly = self.client.get(f"/reports/accounts/profit-and-loss/compare/?mode=monthly&from_date={self.start}"
                                  f"&to_date={self.today}")
        self.assertEqual(len(monthly.context["headers"]), self.today.month)
        centers = self.client.get(f"/reports/accounts/profit-and-loss/compare/?mode=by_center&from_date={self.start}"
                                  f"&to_date={self.today}")
        self.assertEqual(centers.context["headers"][-1], "الإجمالي")
        self.assertEqual(centers.context["result"][-1], Decimal("1500"))
        sheet = self.client.get(f"/reports/accounts/balance-sheet/compare/?to_date={self.today}")
        self.assertEqual(sheet.status_code, 200)
        self.assertTrue(sheet.context["balanced"])
        self.assertEqual(self.client.get("/reports/accounts/cash-flow/").status_code, 200)
