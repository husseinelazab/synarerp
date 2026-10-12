import datetime
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, GLEntry, JournalEntry, PaymentEntry, ZakatDeclaration, withholding_account
from buying.models import Supplier
from core.models import FiscalYear
from core.tenancy import tenant_context
from saas.tests import make_company


def balance(account):
    agg = GLEntry.objects.filter(is_cancelled=False, account=account).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class ZakatWhtTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الزكاة", "zakat")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.year = FiscalYear.objects.get()

    def je(self, debit, credit, amount, date):
        je = JournalEntry.objects.create(posting_date=date)
        je.accounts.create(account=Account.objects.get(number=debit), debit=amount)
        je.accounts.create(account=Account.objects.get(number=credit), credit=amount)
        je.submit()

    def test_withholding_on_supplier_payment(self):
        supplier = Supplier.objects.create(name="شركة استشارات أجنبية", is_non_resident=True, withholding_rate=5,
                                           withholding_type="خدمات فنية واستشارية")
        pay = PaymentEntry.objects.create(payment_type="pay", supplier=supplier, amount=10000,
                                          account=self.company.default_cash_account)
        pay.calculate_totals()
        pay.save()
        pay.submit()
        self.assertEqual(pay.withholding_amount, Decimal("500"))
        self.assertEqual(balance(self.company.default_cash_account), Decimal("-9500"))
        self.assertEqual(balance(withholding_account(self.company)), Decimal("-500"))
        self.client.force_login(self.user)
        page = self.client.get("/reports/accounts/withholding/")
        self.assertEqual(page.context["total_tax"], Decimal("500"))

    def test_zakat_base_and_provision(self):
        start = self.year.start_date
        prev = FiscalYear.objects.create(name="السابقة", start_date=start.replace(year=start.year - 1),
                                         end_date=start - datetime.timedelta(days=1))
        self.je("1102", "3101", 1000000, prev.end_date)          # رأس المال أول السنة
        self.je("1202", "1102", 300000, start)                     # أصول ثابتة تُحسم
        self.je("1102", "2201", 200000, start)                     # قرض طويل الأجل يُضاف
        self.je("1101", "4101", 150000, start)                     # ربح السنة
        z = ZakatDeclaration.objects.create(fiscal_year=self.year)
        z.calculate_totals()
        z.save()
        self.assertEqual((z.equity, z.long_term, z.fixed_assets, z.net_profit),
                         (Decimal("1000000"), Decimal("200000"), Decimal("300000"), Decimal("150000")))
        self.assertEqual(z.zakat_base, Decimal("1050000"))
        self.assertEqual(z.zakat_amount, Decimal("27064.80"))
        z.submit()
        self.assertEqual(balance(Account.objects.get(number="2109")), Decimal("-27064.80"))
        self.assertEqual(self.client.get("/app/zakat/new/").status_code, 302)  # غير مسجل دخول
