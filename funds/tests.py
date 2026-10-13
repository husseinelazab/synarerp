import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Account, Fund, GLEntry, JournalEntry
from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from funds.models import DonationReceipt
from funds.reports import activities_data, fund_balances_data
from saas.tests import make_company

TODAY = datetime.date.today()


class FundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية البر الخيرية", "albir")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.cash = self.company.default_cash_account
        self.aid = Account.objects.get(number="5501")
        self.orphans = Fund.objects.create(name="كفالة الأيتام", fund_type="restricted", donor="مؤسسة خيرية")

    def donate(self, amount, donation_type="general", fund=None):
        receipt = DonationReceipt(donor_name="متبرع", donation_type=donation_type, fund=fund, amount=amount,
                                  account=self.cash)
        receipt.full_clean()
        receipt.save()
        receipt.calculate_totals()
        receipt.submit()
        return receipt

    def spend(self, amount, fund, account=None):
        entry = JournalEntry.objects.create(remarks="صرف مساعدات")
        entry.accounts.create(account=account or self.aid, debit=amount, fund=fund)
        entry.accounts.create(account=self.cash, credit=amount)
        entry.calculate_totals()
        entry.submit()
        return entry

    def test_nonprofit_chart_and_zakat_fund(self):
        for number in ("3201", "4201", "4203", "4209", "5501", "5505"):
            self.assertTrue(Account.objects.filter(number=number, is_group=False).exists(), number)
        self.assertTrue(Fund.objects.filter(name="صندوق الزكاة", fund_type="restricted").exists())

    def test_donations_post_to_income_and_fund(self):
        general = self.donate(1000)
        line = GLEntry.objects.get(voucher_type="donation-receipt", voucher_id=general.pk, credit__gt=0)
        self.assertEqual((line.account.number, line.fund), ("4201", None))
        self.donate(5000, "restricted", self.orphans)
        zakat = self.donate(2000, "zakat")
        self.assertEqual(zakat.fund.name, "صندوق الزكاة")
        self.assertEqual(self.orphans.totals()["balance"], Decimal("5000"))
        zakat.cancel()
        self.assertEqual(Fund.objects.get(name="صندوق الزكاة").balance, 0)

    def test_donation_type_must_match_fund(self):
        with self.assertRaises(ValidationError):
            DonationReceipt(donation_type="restricted", amount=10, account=self.cash).full_clean()
        with self.assertRaises(ValidationError):
            DonationReceipt(donation_type="general", fund=self.orphans, amount=10, account=self.cash).full_clean()
        waqf = Fund.objects.create(name="وقف المسجد", fund_type="endowment")
        with self.assertRaises(ValidationError):
            DonationReceipt(donation_type="restricted", fund=waqf, amount=10, account=self.cash).full_clean()

    def test_restricted_fund_cannot_be_overspent(self):
        self.donate(3000, "restricted", self.orphans)
        self.spend(2000, self.orphans)
        with self.assertRaisesMessage(ValidationError, "لا يكفي"):
            self.spend(1500, self.orphans)
        self.assertEqual(self.orphans.balance, Decimal("1000"))
        self.orphans.allow_overspend = True
        self.orphans.save()
        self.spend(1500, self.orphans)
        self.assertEqual(self.orphans.balance, Decimal("-500"))

    def test_unrestricted_spending_is_not_limited(self):
        self.spend(700, None)

    def test_endowment_principal_and_closed_funds_are_protected(self):
        waqf = Fund.objects.create(name="وقف العمارة", fund_type="endowment")
        self.donate(100000, "endowment", waqf)
        with self.assertRaisesMessage(ValidationError, "وقف لا يُصرف من أصله"):
            self.spend(100, waqf)
        self.donate(500, "restricted", self.orphans)
        self.orphans.end_date = TODAY - datetime.timedelta(days=1)
        self.orphans.save()
        with self.assertRaisesMessage(ValidationError, "انتهت مدة الصرف"):
            self.spend(100, self.orphans)
        self.orphans.is_active = False
        self.orphans.save()
        with self.assertRaisesMessage(ValidationError, "موقوف"):  # القيد المباشر يُرفض عند الترحيل
            self.spend(100, Fund.all_objects.get(pk=self.orphans.pk))

    def test_reports(self):
        waqf = Fund.objects.create(name="وقف البئر", fund_type="endowment")
        self.donate(1000)
        self.donate(4000, "restricted", self.orphans)
        self.donate(9000, "endowment", waqf)
        self.spend(1500, self.orphans)
        self.spend(200, None, account=Account.objects.get(number="5505"))
        start = datetime.date(TODAY.year, 1, 1)
        data = activities_data(start, TODAY)
        income, expense = data["sections"]
        self.assertEqual(income["totals"], [Decimal("1000"), Decimal("4000"), Decimal("9000")])
        self.assertEqual(expense["totals"], [Decimal("200"), Decimal("1500"), Decimal("0")])
        self.assertEqual(data["net"], [Decimal("800"), Decimal("2500"), Decimal("9000")])
        balances = fund_balances_data(start, TODAY)
        by_label = {r["label"]: r["closing"] for r in balances["rows"]}
        self.assertEqual(by_label["كفالة الأيتام"], Decimal("2500"))
        self.assertEqual(by_label["الأموال العامة (بدون صندوق)"], Decimal("800"))
        self.client.force_login(self.user)
        for url in ("/funds/balances/", "/funds/activities/", "/app/fund/", f"/app/fund/{self.orphans.pk}/",
                    "/app/donation-receipt/new/", "/app/donation-receipt/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        html = self.client.get("/funds/activities/").content.decode()
        self.assertIn("سندات التبرعات", html)

    def test_journal_form_shows_fund_for_nonprofit(self):
        self.client.force_login(self.user)
        response = self.client.get("/app/journal-entry/new/")
        self.assertIn("fund", response.context["empty_form"].fields)


class BusinessHidesFundsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("مؤسسة تجارية", "biz")

    def test_fund_fields_and_pages_hidden(self):
        self.client.force_login(self.user)
        response = self.client.get("/app/journal-entry/new/")
        self.assertNotIn("fund", response.context["empty_form"].fields)
        self.assertNotIn("fund", self.client.get("/app/sales-invoice/new/").context["form"].fields)
        self.assertRedirects(self.client.get("/funds/balances/"), "/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/app/fund/"), "/", fetch_redirect_response=False)
        with tenant_context(self.company):
            self.assertFalse(Account.objects.filter(number="4201").exists())
