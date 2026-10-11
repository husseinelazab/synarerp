import datetime
from decimal import Decimal

from django.test import TestCase

from accounts.models import Account, JournalEntry, OpeningAccountBalance
from accounts.zakat import zakat_estimate
from core.models import FiscalYear
from core.tenancy import tenant_context
from saas.tests import make_company


class ZakatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الزكاة", "zakat")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        self.year = FiscalYear.objects.get(start_date__lte=self.today, end_date__gte=self.today)

    def acc(self, number):
        return Account.objects.get(number=number)

    def je(self, debit, credit, amount):
        je = JournalEntry.objects.create(posting_date=self.today)
        je.accounts.create(account=self.acc(debit), debit=amount)
        je.accounts.create(account=self.acc(credit), credit=amount)
        je.calculate_totals()
        je.submit()

    def setup_books(self):
        opening = OpeningAccountBalance.objects.create(posting_date=self.year.start_date)
        opening.lines.create(account=self.acc("1102"), debit=500000)
        opening.lines.create(account=self.acc("3101"), credit=500000)
        opening.submit()
        self.je("1202", "1102", 200000)          # معدات: تُحسم
        self.je("5208", "1204", 20000)           # إهلاك: يُخفض الحسم والربح
        self.je("1102", "2201", 100000)          # قرض طويل الأجل: يُضاف
        self.je("1102", "4102", 80000)           # إيراد
        self.je("1102", "2104", 5000)            # مستحقات متداولة: لا تدخل

    def test_net_assets_method(self):
        self.setup_books()
        e = zakat_estimate(self.company, self.year)
        self.assertEqual(e["adjusted_profit"], Decimal("60000"))
        # الإضافات: رأس المال 500,000 + القرض 100,000 + الربح 60,000؛ الحسميات: المعدات 200,000 − الإهلاك 20,000
        self.assertEqual(e["total_additions"], Decimal("660000"))
        self.assertEqual(e["total_deductions"], Decimal("180000"))
        self.assertEqual(e["base"], Decimal("480000"))
        days = (self.year.end_date - self.year.start_date).days + 1
        self.assertEqual(e["rate"], (Decimal("2.5") * days / 354).quantize(Decimal("0.0001")))
        self.assertEqual(e["zakat"], (Decimal("480000") * e["rate"] / 100).quantize(Decimal("0.01")))
        half = zakat_estimate(self.company, self.year, Decimal(50))
        self.assertEqual(half["zakatable"], Decimal("240000"))

    def test_base_not_below_profit_and_manual_treatment(self):
        self.je("1202", "3103", 300000)          # أصل ممول من جاري الشركاء
        self.je("1102", "4102", 50000)
        loss_base = zakat_estimate(self.company, self.year)
        self.assertEqual(loss_base["net_assets"], Decimal("50000"))
        self.je("1202", "2104", 100000)          # أصل مقابل التزام متداول: الصافي أقل من الربح
        self.assertEqual(zakat_estimate(self.company, self.year)["base"], Decimal("50000"))
        equipment = self.acc("1202")
        equipment.zakat_treatment = "ignore"
        equipment.save()
        self.assertEqual(zakat_estimate(self.company, self.year)["total_deductions"], 0)

    def test_page_and_provision_entry(self):
        self.setup_books()
        self.client.force_login(self.user)
        page = self.client.get("/reports/accounts/zakat/")
        self.assertContains(page, "الوعاء الزكوي")
        zakat = page.context["zakat"]
        response = self.client.post("/reports/accounts/zakat/", {"year": self.year.pk, "share": "100"})
        je = JournalEntry.objects.get(entry_type="adjustment")
        self.assertRedirects(response, je.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(je.total_debit, zakat)
        je.submit()
        again = zakat_estimate(self.company, self.year)
        self.assertEqual(again["zakat"], zakat)                  # مصروف الزكاة لا يغير الوعاء
        self.assertEqual(again["zakat_expense"], zakat)
        self.client.post("/reports/accounts/zakat/", {"year": self.year.pk})
        self.assertEqual(JournalEntry.objects.filter(entry_type="adjustment").count(), 1)
