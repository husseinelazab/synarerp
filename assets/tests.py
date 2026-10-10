import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import Account, GLEntry
from assets.models import Asset, AssetCategory, AssetStatus
from assets.services import dispose, post_depreciation
from core.tenancy import tenant_context
from saas.tests import make_company

YEAR = datetime.date.today().year


def d(month, day):
    return datetime.date(YEAR, month, day)


def balance(name):
    return sum(g.debit - g.credit for g in GLEntry.objects.filter(account__name=name, is_cancelled=False))


class AssetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الأصول", "assetboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.bank = Account.objects.get(number="1102")
        self.category = AssetCategory.objects.get(name="الأجهزة والمعدات")

    def asset(self, gross=12000, months=12, salvage=0, opening=0, start=None, credit=None, submit=True):
        a = Asset.objects.create(asset_name="خادم شبكة", category=self.category, purchase_date=d(1, 10),
                                 gross_amount=gross, salvage_value=salvage, useful_life_months=months,
                                 depreciation_start_date=start or d(1, 1), opening_accumulated_depreciation=opening,
                                 credit_account=credit or self.bank, posting_date=d(1, 10))
        if submit:
            a.submit()
        return a

    def test_defaults_provisioned(self):
        self.assertEqual(AssetCategory.objects.count(), 3)
        self.assertTrue(Account.objects.filter(name="مجمع إهلاك الأصول الثابتة",
                                               account_type="accumulated_depreciation").exists())

    def test_purchase_posts_and_builds_schedule(self):
        a = self.asset(gross=10000, months=3)
        self.assertEqual(balance("الأجهزة والمعدات"), Decimal("10000.00"))
        self.assertEqual(balance("البنك"), Decimal("-10000.00"))
        amounts = [r.amount for r in a.schedule.all()]
        self.assertEqual(amounts, [Decimal("3333.33"), Decimal("3333.33"), Decimal("3333.34")])
        self.assertEqual([r.schedule_date for r in a.schedule.all()], [d(1, 31), d(2, 28 if YEAR % 4 else 29), d(3, 31)])

    def test_monthly_posting_one_entry_per_month(self):
        a = self.asset()
        b = self.asset(gross=6000, months=6)
        entries = post_depreciation(d(2, 28 if YEAR % 4 else 29))
        self.assertEqual(len(entries), 2)  # يناير وفبراير
        self.assertEqual(entries[0].total_debit, Decimal("2000.00"))  # 1000 + 1000
        a.refresh_from_db()
        self.assertEqual((a.accumulated_depreciation, a.book_value), (Decimal("2000.00"), Decimal("10000.00")))
        self.assertEqual(balance("الإهلاك"), Decimal("4000.00"))
        self.assertEqual(balance("مجمع إهلاك الأصول الثابتة"), Decimal("-4000.00"))
        self.assertEqual(post_depreciation(d(2, 28)), [])  # لا تكرار
        # إلغاء الأحدث مسموح ويعيد الأقساط، والأقدم ممنوع قبله
        with self.assertRaisesMessage(ValidationError, "من الأحدث"):
            entries[0].cancel()
        entries[1].cancel()
        a.refresh_from_db()
        self.assertEqual(a.accumulated_depreciation, Decimal("1000.00"))
        self.assertEqual(a.schedule.filter(journal_entry__isnull=True).count(), 11)
        self.assertEqual(b.schedule.count(), 6)

    def test_fully_depreciated_status(self):
        a = self.asset(gross=1200, months=2, salvage=200)
        post_depreciation(d(3, 31))
        a.refresh_from_db()
        self.assertEqual((a.status, a.book_value), (AssetStatus.FULLY_DEPRECIATED, Decimal("200.00")))

    def test_opening_asset_with_prior_depreciation(self):
        opening = Account.objects.get(name="الأرصدة الافتتاحية")
        a = self.asset(gross=50000, months=20, opening=30000, credit=opening)
        self.assertEqual(a.accumulated_depreciation, Decimal("30000.00"))
        self.assertEqual(a.schedule.first().amount, Decimal("1000.00"))  # (50000 − 30000) ÷ 20
        self.assertEqual(balance("مجمع إهلاك الأصول الثابتة"), Decimal("-30000.00"))
        self.assertEqual(balance("الأرصدة الافتتاحية"), Decimal("-20000.00"))

    def test_sale_with_gain_and_vat(self):
        a = self.asset()  # 12000 / 12 شهراً
        je = dispose(a, d(3, 31), Decimal("10000"), self.bank, vat=True, user=self.user)
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.DISPOSED)
        self.assertEqual(a.accumulated_depreciation, Decimal("3000.00"))  # يناير–مارس رُحّلت تلقائياً
        # القيمة الدفترية 9000؛ الثمن 10000 → ربح 1000، والضريبة 1500
        self.assertEqual(balance("أرباح وخسائر استبعاد الأصول"), Decimal("-1000.00"))
        self.assertEqual(balance("ضريبة القيمة المضافة - مخرجات"), Decimal("-1500.00"))
        self.assertEqual(balance("الأجهزة والمعدات"), 0)
        self.assertEqual(balance("مجمع إهلاك الأصول الثابتة"), 0)
        self.assertEqual(post_depreciation(d(12, 31)), [])  # المستبعد لا يُهلك
        with self.assertRaisesMessage(ValidationError, "ألغِ قيد استبعاده"):
            a.schedule.filter(journal_entry__isnull=False).last().journal_entry.cancel()
        je.cancel()
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.ACTIVE)

    def test_write_off_records_loss(self):
        a = self.asset()
        dispose(a, d(1, 31), 0, user=self.user)
        self.assertEqual(balance("أرباح وخسائر استبعاد الأصول"), Decimal("11000.00"))

    def test_cancel_rules(self):
        a = self.asset()
        post_depreciation(d(1, 31))
        with self.assertRaisesMessage(ValidationError, "ألغِ قيود الإهلاك"):
            a.cancel()

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.asset(gross=1000, salvage=800, opening=500)
        supplier_acc = Account.objects.get(number="2101")
        with self.assertRaisesMessage(ValidationError, "حدد المورد"):
            self.asset(credit=supplier_acc)

    def test_pages(self):
        a = self.asset()
        self.assertContains(self.client.get(a.get_absolute_url()), "جدول الإهلاك")
        page = self.client.get(reverse("asset_depreciation") + f"?upto={YEAR}-02-28")
        self.assertContains(page, "خادم شبكة")
        self.client.post(reverse("asset_depreciation"), {"upto": f"{YEAR}-01-31"})
        a.refresh_from_db()
        self.assertEqual(a.accumulated_depreciation, Decimal("1000.00"))
        self.assertContains(self.client.get(reverse("asset_register") + f"?as_of={YEAR}-01-31"), "11,000.00")
        self.assertEqual(self.client.get(reverse("asset_dispose", args=[a.pk])).status_code, 200)
        self.client.post(reverse("asset_dispose", args=[a.pk]), {"date": f"{YEAR}-02-15", "sale_amount": "0"})
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.DISPOSED)
        for slug in ("asset", "asset-category"):
            self.assertEqual(self.client.get(reverse("doc_new", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_import_template", args=[slug, "xlsx"])).status_code, 200)
