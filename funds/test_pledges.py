from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Fund, GLEntry
from beneficiaries.tests import make_nonprofit
from core.models import FiscalYear
from core.tenancy import tenant_context
from funds.models import Donor, Pledge
from funds.statements import build_package

TODAY = timezone.localdate()


class PledgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الوعود", "pledge_owner")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.donor = Donor.objects.create(name="شركة الوفاء")

    def pledge(self, **kwargs):
        doc = Pledge(donor=self.donor, amount=kwargs.pop("amount", 10000), posting_date=TODAY, **kwargs)
        doc.full_clean()
        doc.save()
        doc.calculate_totals()
        doc.save()
        doc.submit()
        return doc

    def gl(self, doc):
        return {e.account.number: (e.debit, e.credit) for e in
                GLEntry.objects.filter(voucher_type=doc.doctype_slug, voucher_id=doc.pk, is_cancelled=False)}

    def test_unconditional_pledge_is_revenue_then_collection_reduces_receivable(self):
        pledge = self.pledge()
        self.assertEqual(self.gl(pledge), {"11602001": (Decimal("10000"), 0), "31201001": (0, Decimal("10000"))})
        receipt = pledge.collect(self.user)
        receipt.amount = Decimal("4000")
        receipt.save()
        receipt.submit()
        self.assertEqual(self.gl(receipt), {self.company.default_cash_account.number: (Decimal("4000"), 0),
                                            "11602001": (0, Decimal("4000"))})
        self.assertEqual(pledge.outstanding, Decimal("6000"))
        too_much = pledge.collect(self.user)
        too_much.amount = Decimal("7000")
        too_much.save()
        with self.assertRaisesMessage(ValidationError, "أكبر من المتبقي"):
            too_much.submit()
        with self.assertRaisesMessage(ValidationError, "ألغِ سندات تحصيل"):
            pledge.cancel()

    def test_restricted_pledge_needs_restricted_fund(self):
        with self.assertRaises(ValidationError):
            self.pledge(restriction="restricted")
        fund = Fund.objects.get(name="صندوق الزكاة")
        pledge = self.pledge(restriction="restricted", fund=fund)
        entry = GLEntry.objects.get(voucher_type="pledge", voucher_id=pledge.pk, credit__gt=0)
        self.assertEqual((entry.account.number, entry.fund), ("31102002", fund))

    def test_conditional_pledge_is_disclosed_until_met(self):
        pledge = self.pledge(is_conditional=True, condition="عند جمع 50% من تكلفة المشروع", amount=50000)
        self.assertEqual(self.gl(pledge), {})
        self.assertEqual(pledge.available_actions, [("condition_met", "تحقق الشرط (إثبات الوعد)")])
        year = FiscalYear.objects.get(start_date__year=TODAY.year)
        notes = {n["title"]: n for n in build_package(self.company, year)["notes"]}
        self.assertEqual(notes["وعود التبرع المشروطة (غير مثبتة)"]["pledges_total"], Decimal("50000"))
        recognized = pledge.condition_met(self.user)
        self.assertTrue(recognized.is_draft)
        self.assertFalse(recognized.is_conditional)
        self.assertEqual(pledge.available_actions, [])
        notes = {n["title"] for n in build_package(self.company, year)["notes"]}
        self.assertNotIn("وعود التبرع المشروطة (غير مثبتة)", notes)
        self.assertIn("أسس الإعداد", build_package(self.company, year)["notes"][1]["text"])

    def test_pages(self):
        pledge = self.pledge()
        self.client.force_login(self.user)
        for url in ("/app/pledge/", "/app/pledge/new/", f"/app/pledge/{pledge.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(f"/app/pledge/{pledge.pk}/"), "تسجيل التحصيل")
