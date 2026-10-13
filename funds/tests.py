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
        self.aid = Account.objects.get(number="42102001")  # مصاريف برامج وأنشطة - كفالات
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

    def test_official_nonprofit_chart(self):
        from accounts.nonprofit_chart import SUPPLEMENTARY
        from assets.models import AssetCategory
        from hr.models import PayrollSettings

        accounts = Account.objects.all()
        self.assertEqual(accounts.count(), 927 + len(SUPPLEMENTARY))
        self.assertEqual(len(set(accounts.values_list("name", flat=True))), accounts.count())
        by_number = {a.number: a for a in accounts}
        self.assertEqual((by_number["231"].root_type, by_number["23"].parent), ("equity", None))
        self.assertEqual(by_number["23101001"].root_type, "equity")
        self.assertEqual(by_number["21302001"].root_type, "liability")
        self.assertEqual((by_number["31101001"].root_type, by_number["31101001"].account_type), ("income",
                                                                                                "income_account"))
        self.assertEqual(by_number["42101001"].root_type, "expense")
        self.assertEqual(by_number["11101002"].account_type, "bank")
        self.assertEqual(by_number["11103001"].account_type, "cash")
        self.assertTrue(by_number["11101"].is_group)
        self.assertEqual(self.company.default_cash_account.number, "11103001")
        self.assertEqual(self.company.default_retained_earnings_account.number, "23101001")
        self.assertIn("(", by_number["21402001"].name)  # اسم مكرر في الدليل يُميز باسم أبيه
        payroll = PayrollSettings.all_objects.get(company=self.company)
        self.assertEqual((payroll.salary_expense_account.number, payroll.gosi_payable_account.number),
                         ("41101001", "21304004"))
        self.assertEqual(AssetCategory.objects.get(name="السيارات").accumulated_depreciation_account.number,
                         "2250100401")
        self.assertFalse(Account.objects.filter(number__in=["4101", "5201"]).exists())  # لا دليل المنشآت التجارية
        self.assertTrue(Fund.objects.filter(name="صندوق الزكاة", fund_type="restricted").exists())
        from accounts.chart import apply_company_chart

        self.assertEqual(apply_company_chart(self.company), (0, []))

    def test_income_account_must_match_donation_branch(self):
        restricted = Account.objects.get(number="31102001")
        with self.assertRaises(ValidationError):
            DonationReceipt(donation_type="general", income_account=restricted, amount=10, account=self.cash,
                            company=self.company).full_clean()

    def test_restricted_branch_without_fund_counts_as_restricted(self):
        self.donate(1000)
        self.spend(300, None)  # 42102001 من فرع البرامج المقيدة
        data = activities_data(datetime.date(TODAY.year, 1, 1), TODAY)
        self.assertEqual(data["sections"][1]["totals"], [Decimal("0"), Decimal("300"), Decimal("0")])

    def test_payroll_posts_to_official_chart(self):
        from hr.models import Employee, PayrollRun

        Employee.objects.create(employee_number="E1", full_name="باحث اجتماعي", gender="male", is_saudi=True,
                                date_of_joining=datetime.date(TODAY.year, 1, 1), basic_salary=6000,
                                housing_allowance=1500)
        run = PayrollRun.objects.create(year=TODAY.year, month=1, posting_date=datetime.date(TODAY.year, 1, 28))
        run.calculate_totals()
        run.save()
        run.submit()
        numbers = set(GLEntry.objects.filter(voucher_type="payroll-run", voucher_id=run.pk)
                      .values_list("account__number", flat=True))
        self.assertTrue({"41101001", "41101008", "21401001", "21304004"} <= numbers, numbers)

    def test_nonprofit_balance_sheet_labels(self):
        self.donate(1000)
        self.client.force_login(self.user)
        html = self.client.get("/reports/accounts/balance-sheet/").content.decode()
        self.assertIn("صافي الأصول", html)
        self.assertIn("فائض (عجز) الفترة", html)
        for url in ("/reports/accounts/chart-of-accounts/", "/reports/accounts/trial-balance/",
                    "/reports/accounts/profit-and-loss/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_donations_post_to_income_and_fund(self):
        general = self.donate(1000)
        line = GLEntry.objects.get(voucher_type="donation-receipt", voucher_id=general.pk, credit__gt=0)
        self.assertEqual((line.account.number, line.fund), ("31201001", None))  # التبرع العام
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
        self.spend(200, None, account=Account.objects.get(number="44103"))  # مصاريف جمع الأموال - المستهلكات
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
            self.assertFalse(Account.objects.filter(number="31201001").exists())
