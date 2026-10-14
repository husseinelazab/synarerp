from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Account, Fund, GLEntry
from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from funds.models import InKindDonation
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse

TODAY = timezone.localdate()


class InKindDonationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية العطاء العيني", "inkind_owner")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.warehouse = Warehouse.objects.get(name="المستودع الرئيسي")
        self.item = Item.objects.create(code="RICE", name="أرز 10 كجم", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first())

    def donation(self, restriction="restricted", fund=None, qty=10, rate=80):
        doc = InKindDonation.objects.create(warehouse=self.warehouse, restriction=restriction, fund=fund,
                                            posting_date=TODAY)
        doc.items.create(item=self.item, qty=qty, rate=rate)
        doc.calculate_totals()
        doc.save()
        return doc

    def gl(self, doc):
        return {e.account.number: (e.debit, e.credit, e.fund_id)
                for e in GLEntry.objects.filter(voucher_type=doc.doctype_slug, voucher_id=doc.pk, is_cancelled=False)}

    def test_restricted_donation_enters_stock_at_fair_value(self):
        fund = Fund.objects.get(name="صندوق الزكاة")
        doc = self.donation(fund=fund)
        doc.submit()
        self.assertEqual(doc.total, Decimal("800"))
        self.assertEqual(doc.income_account.number, "31103003")
        stock_account = self.company.default_stock_account.number
        self.assertEqual(self.gl(doc), {stock_account: (Decimal("800"), 0, None),
                                        "31103003": (0, Decimal("800"), fund.pk)})
        bin_ = Bin.objects.get(item=self.item, warehouse=self.warehouse)
        self.assertEqual((bin_.actual_qty, bin_.stock_value), (Decimal("10"), Decimal("800")))
        doc.cancel()
        bin_.refresh_from_db()
        self.assertEqual(bin_.actual_qty, 0)
        self.assertEqual(self.gl(doc), {})

    def test_unrestricted_account_and_validation(self):
        doc = self.donation("unrestricted", qty=2, rate=50)
        doc.submit()
        self.assertEqual(doc.income_account.number, "31202001")
        with self.assertRaises(ValidationError):  # غير المقيد لا يذهب لصندوق مقيد
            self.donation("unrestricted", fund=Fund.objects.get(name="صندوق الزكاة")).full_clean()
        wrong = self.donation("restricted")
        wrong.income_account = Account.objects.get(number="31202001")
        with self.assertRaises(ValidationError):  # المقيد في فرع 311 فقط
            wrong.full_clean()
        zero = self.donation(rate=0)
        with self.assertRaises(ValidationError):
            zero.submit()

    def test_pages(self):
        doc = self.donation()
        self.client.force_login(self.user)
        for url in ("/app/in-kind-donation/", "/app/in-kind-donation/new/", f"/app/in-kind-donation/{doc.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
