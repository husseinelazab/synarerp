from django.test import TestCase
from django.urls import reverse

from accounts.models import PaymentEntry, PaymentType
from buying.models import Supplier
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


class VoucherSeparationTests(TestCase):
    """سندات القبض والصرف: ترقيم وعناوين وقوائم مستقلة لكل نوع."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة السندات", "vouchers")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.owner)
        self.customer = Customer.objects.create(name="عميل السندات")
        self.supplier = Supplier.objects.create(name="مورد السندات")

    def voucher(self, payment_type, **party):
        doc = PaymentEntry.objects.create(payment_type=payment_type, account=self.company.default_cash_account,
                                          amount=100, **party)
        doc.submit()
        return doc

    def test_separate_numbering_and_titles(self):
        receipt = self.voucher(PaymentType.RECEIVE, customer=self.customer)
        payment = self.voucher(PaymentType.PAY, supplier=self.supplier)
        self.assertTrue(receipt.name.startswith("RV-"))
        self.assertTrue(payment.name.startswith("PV-"))
        self.assertTrue(self.voucher(PaymentType.PAY, supplier=self.supplier).name.endswith("00002"))
        self.assertContains(self.client.get(payment.get_absolute_url()), f"سند صرف: {payment.name}")
        self.assertContains(self.client.get(receipt.get_absolute_url()), f"سند قبض: {receipt.name}")

    def test_lists_are_separate(self):
        receipt = self.voucher(PaymentType.RECEIVE, customer=self.customer)
        payment = self.voucher(PaymentType.PAY, supplier=self.supplier)
        url = reverse("doc_list", args=["payment-entry"])
        page = self.client.get(url + "?payment_type=pay")
        self.assertContains(page, "<title>سندات الصرف")
        self.assertContains(page, payment.name)
        self.assertNotContains(page, receipt.name)
        self.assertContains(page, "?payment_type=pay")  # زر «سند صرف جديد» يحمل النوع
        page = self.client.get(url + "?payment_type=receive")
        self.assertContains(page, "<title>سندات القبض")
        self.assertNotContains(page, payment.name)

    def test_new_form_fixes_the_type(self):
        page = self.client.get(reverse("doc_new", args=["payment-entry"]) + "?payment_type=pay")
        self.assertContains(page, "سند صرف جديد")
        self.assertContains(page, 'type="hidden" name="payment_type" value="pay"')
        page = self.client.get(reverse("doc_new", args=["payment-entry"]))
        self.assertContains(page, '<select name="payment_type"')  # بلا نوع في الرابط يبقى الاختيار متاحاً

    def test_menu_has_both(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("سندات القبض", page)
        self.assertIn("سندات الصرف", page)
        self.assertNotIn("سندات القبض والصرف", page)
