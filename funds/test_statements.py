import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Account, Fund, GLEntry, JournalEntry, PeriodClosingVoucher
from beneficiaries.tests import make_nonprofit
from core.models import FiscalYear
from core.tenancy import tenant_context
from funds.models import DonationReceipt
from funds.statements import CLASS_KEYS, build_package
from saas.tests import make_company


class StatementsPackageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية القوائم المالية", "fs_owner")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        today = timezone.localdate()
        self.year = FiscalYear.objects.get(start_date__year=today.year)
        prior = today.year - 1
        self.prior_year = FiscalYear.objects.create(name=str(prior), start_date=datetime.date(prior, 1, 1),
                                                    end_date=datetime.date(prior, 12, 31))
        self.cash = self.company.default_cash_account
        self.zakat = Fund.objects.get(name="صندوق الزكاة")

    def account(self, number):
        return Account.objects.get(number=number)

    def donate(self, amount, date, donation_type="general", fund=None):
        receipt = DonationReceipt(donation_type=donation_type, fund=fund, amount=amount, account=self.cash,
                                  posting_date=date)
        receipt.full_clean()
        receipt.save()
        receipt.calculate_totals()
        receipt.submit()

    def journal(self, date, *lines):
        entry = JournalEntry.objects.create(posting_date=date)
        for number, debit, credit, fund in lines:
            entry.accounts.create(account=self.account(number), debit=debit, credit=credit, fund=fund)
        entry.calculate_totals()
        entry.submit()

    def test_two_years_with_closing(self):
        p = self.prior_year.start_date
        c = self.year.start_date
        # السنة السابقة: تبرع عام وزكاة وأثاث وإهلاك ثم الإقفال حسب الفئات
        self.donate(20000, p + datetime.timedelta(days=10))
        self.donate(8000, p + datetime.timedelta(days=20), "zakat")
        self.journal(p + datetime.timedelta(days=30), ("12105001", 6000, 0, None), ("11103001", 0, 6000, None))
        self.journal(p + datetime.timedelta(days=300), ("41305001", 600, 0, None), ("22501005", 0, 600, None))
        PeriodClosingVoucher.objects.create(fiscal_year=self.prior_year).submit()
        # السنة الحالية: صرف زكاة، مخصص نهاية خدمة، قرض
        self.journal(c + datetime.timedelta(days=5), ("42101001", 3000, 0, self.zakat), ("11103001", 0, 3000, None))
        self.journal(c + datetime.timedelta(days=6), ("41102020", 400, 0, None), ("22401001", 0, 400, None))
        self.journal(c + datetime.timedelta(days=7), ("11101001", 10000, 0, None), ("22101003", 0, 10000, None))

        package = build_package(self.company, self.year)
        self.assertEqual(package["checks"], {"balanced": True, "cash": True, "net_assets": True})
        position, activities = package["position"], package["activities"]
        net = {n["key"]: (n["value"], n["prior"]) for n in position["net_assets"]}
        self.assertEqual(net["unrestricted"], (Decimal("19000"), Decimal("19400")))  # 20000 − 600 ثم − 400
        self.assertEqual(net["restricted"], (Decimal("5000"), Decimal("8000")))      # زكاة 8000 ثم صرف 3000
        self.assertEqual([activities["opening"][k] for k in CLASS_KEYS],
                         [Decimal("19400"), Decimal("8000"), Decimal("0")])
        self.assertEqual(activities["change_prior"], Decimal("27400"))
        self.assertEqual(position["total_assets"], Decimal("34400"))                  # نقد 29000 + أثاث صافي 5400
        flows = package["flows"]
        self.assertEqual((flows["opening"], flows["closing"]), (Decimal("22000"), Decimal("29000")))
        self.assertEqual(flows["financing"], Decimal("10000"))
        self.assertEqual(package["flows_prior"]["investing"], Decimal("-6000"))
        notes = {n["title"]: n for n in package["notes"]}
        schedule = notes["الممتلكات والآلات والمعدات - بالصافي"]["schedule"]["totals"]
        self.assertEqual((schedule["cost_close"], schedule["dep_close"], schedule["nbv"]),
                         (Decimal("6000"), Decimal("600"), Decimal("5400")))
        provision = notes["المخصصات (مكافأة نهاية الخدمة وغيرها)"]["movement"]
        self.assertEqual((provision["opening"], provision["additions"], provision["closing"]),
                         (0, Decimal("400"), Decimal("400")))
        restricted_note = notes["صافي الأصول المقيدة حسب الصندوق"]
        self.assertEqual((restricted_note["total"], restricted_note["total_prior"]), (Decimal("5000"), Decimal("8000")))
        expenses = notes["مصروفات البرامج والأنشطة المقيدة حسب الغرض"]
        self.assertEqual(expenses["total"], activities["sections"][1]["totals"]["restricted"])

    def test_pages_and_excel(self):
        self.donate(1000, self.year.start_date)
        self.client.force_login(self.user)
        response = self.client.get(f"/funds/statements/?year={self.year.pk}")
        self.assertContains(response, "قائمة المركز المالي")
        self.assertContains(response, "قائمة التدفقات النقدية")
        self.assertContains(response, "✓ قائمة المركز المالي متوازنة")
        export = self.client.get(f"/funds/statements/export/?year={self.year.pk}")
        self.assertEqual(export.status_code, 200)
        import io

        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(export.content))
        self.assertEqual(book.sheetnames, ["قائمة المركز المالي", "قائمة الأنشطة", "قائمة التدفقات النقدية",
                                           "الإيضاحات"])
        self.assertContains(self.client.get("/reports/accounts/trial-balance/"), "حزمة القوائم المالية")

    def test_business_cannot_open(self):
        _, user = make_company("منشأة تجارية للقوائم", "fs_biz")
        self.client.force_login(user)
        self.assertRedirects(self.client.get("/funds/statements/"), "/", fetch_redirect_response=False)

    def test_waqf_classification_follows_socpa(self):
        c = self.year.start_date
        # أصل الوقف: تبرع لإنشاء وقف ثم شراء مبنى وقفي
        self.journal(c, ("13101001", 100000, 0, None), ("31301003", 0, 100000, None))
        self.journal(c + datetime.timedelta(days=1), ("13402001", 100000, 0, None), ("13101001", 0, 100000, None))
        # غلة وقف دون شرط بمصارف محددة: غير مقيدة
        self.journal(c + datetime.timedelta(days=2), ("11103001", 5000, 0, None), ("31303001", 0, 5000, None))
        # استهلاك المبنى الوقفي
        self.journal(c + datetime.timedelta(days=3), ("43103001", 2000, 0, None), ("2270100201", 0, 2000, None))

        package = build_package(self.company, self.year)
        self.assertEqual(package["checks"], {"balanced": True, "cash": True, "net_assets": True})
        activities = package["activities"]
        self.assertEqual(dict(activities["closing"]), {"unrestricted": Decimal("5000"), "restricted": Decimal("0"),
                                                      "endowment": Decimal("98000")})
        expense = activities["sections"][1]
        depreciation = next(l for l in expense["lines"] if l["code"] == "43103")
        self.assertEqual(dict(depreciation["values"]), {"unrestricted": Decimal("2000"), "restricted": Decimal("0"),
                                                        "endowment": Decimal("0")})
        reclass = activities["transfers"][-1]
        self.assertIn("استهلاك أصول الأوقاف", reclass["label"])
        self.assertEqual((reclass["values"]["unrestricted"], reclass["values"]["endowment"]),
                         (Decimal("2000"), Decimal("-2000")))
        PeriodClosingVoucher.objects.create(fiscal_year=self.year).submit()
        closed = {n: sum((e.credit - e.debit for e in GLEntry.objects.filter(account__number=n, is_cancelled=False)),
                         Decimal(0)) for n in ("23101001", "23103001")}
        self.assertEqual(closed, {"23101001": Decimal("5000"), "23103001": Decimal("98000")})
