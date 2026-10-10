import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, GLEntry, OpeningAccountBalance, OpeningPartyBalance
from buying.models import Supplier
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer
from stock.models import UOM, Bin, Item, ItemGroup, OpeningStock, Warehouse


def balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class OpeningBalanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الافتتاح", "opener")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()

    def acc(self, number):
        return Account.objects.get(number=number)

    def test_full_migration_scenario(self):
        # 1) الحسابات: بنك 50,000 + أثاث 20,000 مدين، قرض 30,000 دائن
        accounts = OpeningAccountBalance.objects.create(posting_date=self.today)
        accounts.lines.create(account=self.acc("1102"), debit=50000)
        accounts.lines.create(account=self.acc("1201"), debit=20000)
        accounts.lines.create(account=self.acc("2201"), credit=30000)
        accounts.submit()
        # 2) العملاء والموردون
        customers = OpeningPartyBalance.objects.create(party_type="customer", posting_date=self.today)
        customers.lines.create(customer=Customer.objects.create(name="عميل قديم"), amount=8000, reference="INV-OLD-77")
        customers.submit()
        suppliers = OpeningPartyBalance.objects.create(party_type="supplier", posting_date=self.today)
        supplier = Supplier.objects.create(name="مورد قديم")
        suppliers.lines.create(supplier=supplier, amount=5000)
        suppliers.submit()
        # 3) المخزون
        item = Item.objects.create(code="OP1", name="صنف افتتاحي", item_group=ItemGroup.objects.first(),
                                   uom=UOM.objects.first())
        stock = OpeningStock.objects.create(warehouse=Warehouse.objects.first(), posting_date=self.today)
        stock.items.create(item=item, qty=10, rate=150)
        stock.submit()

        self.assertEqual(balance(), 0)
        self.assertEqual(balance(account=self.acc("1102")), Decimal("50000"))
        self.assertEqual(balance(customer__name="عميل قديم"), Decimal("8000"))
        self.assertEqual(balance(supplier=supplier), Decimal("-5000"))
        self.assertEqual(Bin.objects.get(item=item).stock_value, Decimal("1500"))
        self.assertEqual(balance(account=self.company.default_stock_account), Decimal("1500"))
        # حساب الأرصدة الافتتاحية = صافي حقوق الملكية = 50,000 + 20,000 - 30,000 + 8,000 - 5,000 + 1,500
        self.assertEqual(-balance(account=self.company.default_opening_account), Decimal("44500"))
        self.assertEqual(GLEntry.objects.filter(remarks="INV-OLD-77").count(), 1)

    def test_receivable_payable_and_stock_accounts_must_use_their_tools(self):
        for number in ("1103", "2101", "1104"):
            doc = OpeningAccountBalance.objects.create(posting_date=self.today)
            doc.lines.create(account=self.acc(number), debit=100)
            with self.assertRaises(ValidationError):
                doc.submit()

    def test_party_type_mismatch_rejected(self):
        doc = OpeningPartyBalance.objects.create(party_type="customer", posting_date=self.today)
        doc.lines.create(supplier=Supplier.objects.create(name="س"), amount=100)
        with self.assertRaises(ValidationError):
            doc.submit()

    def test_cancel_reverses(self):
        doc = OpeningAccountBalance.objects.create(posting_date=self.today)
        doc.lines.create(account=self.acc("1101"), debit=700)
        doc.submit()
        doc.cancel()
        self.assertEqual(balance(account=self.acc("1101")), 0)

    def test_wizard_and_import(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/reports/accounts/opening/"), "معالج الأرصدة الافتتاحية")
        for slug in ("opening-account", "opening-party", "opening-stock"):
            self.assertEqual(self.client.get(f"/app/{slug}/template.xlsx").status_code, 200)
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200)


class PeriodClosingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الإقفال", "closer")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        from core.models import FiscalYear
        self.year = FiscalYear.objects.get()
        self.date = self.year.start_date + datetime.timedelta(days=10)

    def post(self, debit_account, credit_account, amount):
        from accounts.models import JournalEntry
        je = JournalEntry.objects.create(posting_date=self.date)
        je.accounts.create(account=Account.objects.get(number=debit_account), debit=amount)
        je.accounts.create(account=Account.objects.get(number=credit_account), credit=amount)
        je.submit()

    def test_closing_moves_profit_to_retained_earnings_and_locks_year(self):
        from accounts.models import JournalEntry, PeriodClosingVoucher
        self.post("1101", "4101", 10000)   # إيراد
        self.post("5202", "1101", 3000)    # مصروف إيجار
        pcv = PeriodClosingVoucher.objects.create(fiscal_year=self.year)
        pcv.submit()
        self.assertEqual(pcv.posting_date, self.year.end_date)
        self.assertEqual(pcv.net_profit, Decimal("7000"))
        self.assertEqual(balance(account__number="4101"), 0)
        self.assertEqual(balance(account__number="5202"), 0)
        self.assertEqual(-balance(account__number="3102"), Decimal("7000"))
        self.assertEqual(balance(), 0)
        self.year.refresh_from_db()
        self.assertTrue(self.year.is_closed)
        with self.assertRaises(ValidationError):  # لا ترحيل في سنة مقفلة
            self.post("1101", "4101", 1)
        # قائمة الدخل للسنة المقفلة تبقى تعرض النتيجة الحقيقية
        self.client.force_login(self.user)
        report = self.client.get(f"/reports/accounts/profit-and-loss/?from_date={self.year.start_date}&to_date={self.year.end_date}")
        self.assertEqual(report.context["result"], Decimal("7000"))
        sheet = self.client.get(f"/reports/accounts/balance-sheet/?to_date={self.year.end_date}")
        self.assertTrue(sheet.context["balanced"])
        # لا يُقفل مرتين، والإلغاء يعيد فتح السنة ويعكس القيد
        with self.assertRaises(ValidationError):
            PeriodClosingVoucher.objects.create(fiscal_year=self.year).submit()
        pcv.cancel()
        self.year.refresh_from_db()
        self.assertFalse(self.year.is_closed)
        self.assertEqual(-balance(account__number="4101"), Decimal("10000"))
        self.assertEqual(JournalEntry.objects.filter(docstatus=1).count(), 2)  # المحاولة المرفوضة بقيت مسودة

    def test_loss_is_debited_to_retained_earnings(self):
        from accounts.models import PeriodClosingVoucher
        self.post("5202", "1101", 500)
        pcv = PeriodClosingVoucher.objects.create(fiscal_year=self.year)
        pcv.submit()
        self.assertEqual(pcv.net_profit, Decimal("-500"))
        self.assertEqual(balance(account__number="3102"), Decimal("500"))
