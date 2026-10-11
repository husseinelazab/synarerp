import datetime
from decimal import Decimal

from django.test import TestCase

from accounts.models import Account, JournalEntry, OpeningAccountBalance
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


class CashFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة التدفقات", "cashflow")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        self.client.force_login(self.user)

    def acc(self, number):
        return Account.objects.get(number=number)

    def je(self, debit, credit, amount, **party):
        je = JournalEntry.objects.create(posting_date=self.today)
        je.accounts.create(account=self.acc(debit), debit=amount, customer=party.get("debit_customer"))
        je.accounts.create(account=self.acc(credit), credit=amount, customer=party.get("credit_customer"))
        je.calculate_totals()
        je.submit()

    def test_indirect_statement_reconciles_to_cash(self):
        from core.models import FiscalYear
        earlier = self.today - datetime.timedelta(days=400)
        FiscalYear.objects.create(name="سابقة", start_date=earlier, end_date=FiscalYear.objects.order_by("start_date")
                                  .first().start_date - datetime.timedelta(days=1))
        opening = OpeningAccountBalance.objects.create(posting_date=earlier)
        opening.lines.create(account=self.acc("1102"), debit=50000)               # نقدية قبل الفترة
        opening.submit()
        customer = Customer.objects.create(name="عميل")
        self.je("1103", "4102", 10000, debit_customer=customer)                    # إيراد آجل
        self.je("1102", "1103", 6000, credit_customer=customer)                    # تحصيل جزئي
        self.je("5202", "1101", 2000)                                              # إيجار نقداً
        self.je("5208", "1204", 500)                                               # إهلاك (غير نقدي)
        self.je("1202", "1102", 15000)                                             # شراء معدات
        self.je("1102", "2201", 20000)                                             # قرض
        self.je("3103", "1102", 3000)                                              # مسحوبات الشركاء
        self.je("1101", "1102", 1000)                                              # تحويل داخلي بين النقدية
        ctx = self.client.get("/reports/accounts/cash-flow/", {"from_date": (self.today - datetime.timedelta(days=30)).isoformat(),
                                                                "to_date": self.today.isoformat()}).context
        sections = {s["key"]: s for s in ctx["sections"]}
        self.assertEqual(ctx["profit"], Decimal("7500"))                           # 10000 − 2000 − 500
        self.assertEqual([r["amount"] for r in ctx["adjustments"]], [Decimal("500")])
        # التشغيل: 7500 + 500 − الزيادة في العملاء 4000 = 4000 (= 6000 تحصيل − 2000 إيجار)
        self.assertEqual(sections["operating"]["total"], Decimal("4000"))
        self.assertEqual(sections["investing"]["total"], Decimal("-15000"))
        self.assertEqual(sections["financing"]["total"], Decimal("17000"))
        self.assertEqual(ctx["net_change"], Decimal("6000"))
        self.assertEqual((ctx["opening"], ctx["closing"]), (Decimal("50000"), Decimal("56000")))
        self.assertTrue(ctx["balanced"])

    def test_manual_category_and_page(self):
        other = self.acc("2104")
        other.cash_flow_category = "financing"
        other.save()
        self.je("1102", "2104", 700)
        response = self.client.get("/reports/accounts/cash-flow/")
        sections = {s["key"]: s for s in response.context["sections"]}
        self.assertEqual(sections["financing"]["total"], Decimal("700"))
        self.assertContains(response, "مطابق لرصيد حسابات الصندوق والبنوك")
