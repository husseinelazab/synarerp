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


class CostCenterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الفروع", "branches")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_income_tagged_by_branch_and_pl_filter(self):
        from accounts.models import CostCenter
        from selling.models import SalesInvoice
        root = CostCenter.objects.get(name="جميع المراكز")
        riyadh = CostCenter.objects.create(name="فرع الرياض", parent=root)
        jeddah = CostCenter.objects.create(name="فرع جدة", parent=root)
        service = Item.objects.create(code="SV", name="خدمة", item_group=ItemGroup.objects.first(),
                                      uom=UOM.objects.first(), is_stock_item=False)
        customer = Customer.objects.create(name="ع")
        for center, rate in ((riyadh, 1000), (jeddah, 400)):
            inv = SalesInvoice.objects.create(customer=customer, update_stock=False, cost_center=center)
            inv.items.create(item=service, qty=1, rate=rate)
            inv.submit()
        # الإيراد عليه مركز التكلفة، وحساب العميل والضريبة بلا مركز
        self.assertEqual(-balance(cost_center=riyadh), Decimal("1000"))
        self.assertFalse(GLEntry.objects.filter(account__account_type="receivable", cost_center__isnull=False).exists())
        self.client.force_login(self.user)
        url = "/reports/accounts/profit-and-loss/"
        self.assertEqual(self.client.get(url, {"cost_center": riyadh.pk}).context["result"], Decimal("1000"))
        self.assertEqual(self.client.get(url, {"cost_center": jeddah.pk}).context["result"], Decimal("400"))
        self.assertEqual(self.client.get(url, {"cost_center": root.pk}).context["result"], Decimal("1400"))
        ledger = self.client.get("/reports/accounts/general-ledger/", {"cost_center": jeddah.pk}).context
        self.assertEqual(ledger["total_credit"], Decimal("400"))

    def test_default_center_and_group_center_rejected(self):
        from accounts.models import CostCenter, JournalEntry
        je = JournalEntry.objects.create()
        je.accounts.create(account=Account.objects.get(number="5202"), debit=300)
        je.accounts.create(account=Account.objects.get(number="1101"), credit=300)
        je.submit()
        self.assertEqual(GLEntry.objects.get(account__number="5202").cost_center, self.company.default_cost_center)
        bad = JournalEntry.objects.create()
        bad.accounts.create(account=Account.objects.get(number="5202"), debit=1,
                            cost_center=CostCenter.objects.get(name="جميع المراكز"))
        bad.accounts.create(account=Account.objects.get(number="1101"), credit=1)
        with self.assertRaises(ValidationError):
            bad.submit()


class BankReconciliationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة البنك", "banker")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.bank = Account.objects.get(number="1102")
        self.day = datetime.date.today().replace(day=1)

    def je(self, debit, credit, amount, day_offset=0):
        from accounts.models import JournalEntry
        je = JournalEntry.objects.create(posting_date=self.day + datetime.timedelta(days=day_offset))
        je.accounts.create(account=Account.objects.get(number=debit), debit=amount)
        je.accounts.create(account=Account.objects.get(number=credit), credit=amount)
        je.submit()
        return je

    def test_auto_match_and_reconciliation_statement(self):
        from accounts.bank import auto_match, reconciliation_summary
        from accounts.models import BankTransaction
        self.je("1102", "3101", 10000)               # إيداع رأس مال
        self.je("5202", "1102", 2000, day_offset=1)  # شيك إيجار (صُرف)
        self.je("5205", "1102", 700, day_offset=2)   # شيك لم يُصرف بعد
        self.je("1102", "4103", 500, day_offset=3)   # إيداع بالطريق
        BankTransaction.objects.create(account=self.bank, date=self.day + datetime.timedelta(days=1), deposit=10000)
        BankTransaction.objects.create(account=self.bank, date=self.day + datetime.timedelta(days=4), withdrawal=2000)
        BankTransaction.objects.create(account=self.bank, date=self.day + datetime.timedelta(days=5), withdrawal=25,
                                       description="عمولة بنكية")
        to_date = self.day + datetime.timedelta(days=6)
        self.assertEqual(auto_match(self.bank, to_date), 2)
        summary = reconciliation_summary(self.bank, to_date, statement_balance=Decimal("7975"))
        self.assertEqual(summary["book_balance"], Decimal("7800"))
        self.assertEqual(summary["in_transit"], Decimal("500"))
        self.assertEqual(summary["outstanding"], Decimal("700"))
        self.assertEqual(summary["expected"], Decimal("8000"))
        self.assertEqual(summary["difference"], Decimal("-25"))  # العمولة البنكية غير المسجلة
        # تسجيل العمولة ثم مطابقتها يجعل الفرق صفراً
        self.je("5207", "1102", 25, day_offset=5)
        self.assertEqual(auto_match(self.bank, to_date), 1)
        self.assertEqual(reconciliation_summary(self.bank, to_date, Decimal("7975"))["difference"], 0)

    def test_manual_match_rules_and_cancel_unmatches(self):
        from accounts.bank import match
        from accounts.models import BankTransaction
        je = self.je("1102", "3101", 300)
        gle = GLEntry.objects.get(voucher_id=je.pk, voucher_type="journal-entry", account=self.bank)
        wrong = BankTransaction.objects.create(account=self.bank, date=self.day, deposit=301)
        with self.assertRaises(ValidationError):
            match(gle, wrong)
        line = BankTransaction.objects.create(account=self.bank, date=self.day, deposit=300)
        match(gle, line)
        je.cancel()
        line.refresh_from_db()
        self.assertIsNone(line.matched_entry)

    def test_page_and_statement_import(self):
        self.client.force_login(self.user)
        self.je("1102", "3101", 1000)
        response = self.client.get("/reports/accounts/bank-reconciliation/", {"statement_balance": "1000"})
        self.assertContains(response, "بيان التسوية")
        response = self.client.post("/reports/accounts/bank-reconciliation/", {"action": "auto", "account": self.bank.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/app/bank-transaction/template.xlsx").status_code, 200)
