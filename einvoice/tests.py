import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from lxml import etree

from core.models import Membership, Role
from core.tenancy import tenant_context
from einvoice import api
from einvoice.models import FIRST_INVOICE_PIH, EInvoice, EInvoiceSettings, OnboardingStatus
from einvoice.testing import FakeZatca
from einvoice.xml import NS, decode_qr, invoice_hash
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse

# ترتيب العناصر في جذر الفاتورة حسب مخطط UBL 2.1
UBL_ORDER = ["UBLExtensions", "ProfileID", "ID", "UUID", "IssueDate", "IssueTime", "InvoiceTypeCode",
             "DocumentCurrencyCode", "TaxCurrencyCode", "BillingReference", "AdditionalDocumentReference", "Signature",
             "AccountingSupplierParty", "AccountingCustomerParty", "Delivery", "PaymentMeans", "AllowanceCharge",
             "TaxTotal", "LegalMonetaryTotal", "InvoiceLine"]


class EInvoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("مؤسسة الفوترة", "zatca_owner")
        cls.company.tax_id, cls.company.commercial_register = "310122393500003", "1010010000"
        cls.company.save()

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.fake = FakeZatca()
        patcher = mock.patch.object(api, "_urlopen", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.user)
        self.item = Item.objects.create(code="SV", name="خدمة استشارية", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), is_stock_item=False, selling_rate=100)

    # ---------- أدوات ----------
    def onboard(self):
        data = {"action": "save", "environment": "sandbox", "auto_submit": "on", "seller_name": "مؤسسة الفوترة",
                "vat_number": "310122393500003", "crn": "1010010000", "street": "طريق الملك فهد",
                "building_number": "1234", "district": "العليا", "city": "الرياض", "postal_code": "12345",
                "branch_name": "الفرع الرئيسي", "industry": "تجارة", "invoice_types": "1100"}
        self.assertRedirects(self.client.post("/einvoice/settings/", data), "/einvoice/settings/")
        self.client.post("/einvoice/settings/", {"action": "compliance", "otp": "123345"})
        self.client.post("/einvoice/settings/", {"action": "checks"})
        self.client.post("/einvoice/settings/", {"action": "activate"})
        return EInvoiceSettings.objects.get()

    def sale(self, customer=None, qty=1, rate=100, **kwargs):
        invoice = SalesInvoice.objects.create(customer=customer or Customer.objects.get(name="عميل نقدي"),
                                              update_stock=False, **kwargs)
        invoice.items.create(item=self.item, qty=qty, rate=rate)
        with self.captureOnCommitCallbacks(execute=True):
            invoice.submit()
        invoice.refresh_from_db()
        return invoice

    def b2b_customer(self, **overrides):
        values = dict(name="شركة العميل", tax_id="300000000000003", street="شارع التحلية", building_number="5678",
                      district="الروضة", city="جدة", postal_code="23456")
        values.update(overrides)
        return Customer.objects.create(**values)

    # ---------- الاختبارات ----------
    def test_onboarding_flow_issues_certificates_and_passes_checks(self):
        settings = self.onboard()
        self.assertEqual(settings.status, OnboardingStatus.ACTIVE)
        self.assertTrue(settings.is_active)
        results = settings.compliance_results["results"]
        self.assertEqual(len(results), 6)  # فاتورة + دائن + مدين لكل من الضريبية والمبسطة
        self.assertTrue(all(r["passed"] for r in results))
        paths = [call[0] for call in self.fake.calls]
        self.assertEqual(paths[0], "compliance")
        self.assertEqual(paths.count("compliance/invoices"), 6)
        self.assertEqual(paths[-1], "production/csids")
        self.assertEqual(self.fake.calls[0][1]["Otp"], "123345")
        self.assertNotIn("BEGIN", settings.private_key)  # المفتاح مخزن مشفراً
        self.assertEqual((settings.last_icv, settings.last_hash), (0, FIRST_INVOICE_PIH))

    def test_resubmit_redirects_only_inside_the_site(self):
        self.onboard()
        einvoice = EInvoice.objects.get(sales_invoice=self.sale())
        url = f"/einvoice/{einvoice.pk}/resubmit/"
        self.assertRedirects(self.client.post(url, {"next": "https://evil.example/"}), "/einvoice/",
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.post(url, {"next": "//evil.example/"}), "/einvoice/",
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.post(url, {"next": "/app/sales-invoice/"}), "/app/sales-invoice/",
                             fetch_redirect_response=False)

    def test_simplified_invoice_reported_with_chain_and_qr(self):
        self.onboard()
        first = self.sale().einvoice
        self.assertEqual(first.invoice_type, "simplified")
        self.assertEqual(first.status, "reported")
        self.assertEqual((first.icv, first.previous_hash), (1, FIRST_INVOICE_PIH))
        second = self.sale(qty=2).einvoice
        self.assertEqual((second.icv, second.previous_hash), (2, first.invoice_hash))
        qr = decode_qr(first.qr)
        self.assertEqual(sorted(qr), list(range(1, 10)))
        self.assertEqual(qr[2].decode(), "310122393500003")
        self.assertEqual(qr[4].decode(), "115.00")
        self.assertEqual(invoice_hash(first.xml), first.invoice_hash)
        root = etree.fromstring(first.xml.encode())
        self.assertEqual(root.find(f"{{{NS['cbc']}}}InvoiceTypeCode").get("name"), "0200000")
        order = [etree.QName(child).localname for child in root]
        self.assertEqual(order, sorted(order, key=UBL_ORDER.index))

    def test_standard_invoice_requires_buyer_address_and_is_cleared(self):
        self.onboard()
        incomplete = self.b2b_customer(name="عميل ناقص", building_number="", postal_code="")
        draft = SalesInvoice.objects.create(customer=incomplete, update_stock=False)
        draft.items.create(item=self.item, qty=1, rate=100)
        with self.assertRaises(ValidationError) as ctx:
            draft.submit()
        self.assertIn("رقم مبنى العميل", " ".join(ctx.exception.messages))
        draft.refresh_from_db()
        self.assertTrue(draft.is_draft)
        self.assertEqual(EInvoiceSettings.objects.get().last_icv, 0)  # لم يُستهلك رقم من السلسلة
        invoice = self.sale(customer=self.b2b_customer())
        einvoice = invoice.einvoice
        self.assertEqual((einvoice.invoice_type, einvoice.status), ("standard", "cleared"))
        self.assertTrue(einvoice.cleared_xml)
        self.assertEqual(self.fake.calls[-1][1]["Clearance-status"], "1")
        page = self.client.get(f"/app/sales-invoice/{invoice.pk}/")
        self.assertContains(page, "تم الاعتماد (Clearance)")
        self.assertContains(self.client.get(f"/app/sales-invoice/{invoice.pk}/print/"), "<svg")

    def test_credit_note_references_original_and_cancel_is_blocked(self):
        self.onboard()
        original = self.sale(customer=self.b2b_customer(), qty=3)
        with self.assertRaises(ValidationError):
            original.cancel()
        credit = original.make_return(quantities={self.item.id: 1})
        with self.captureOnCommitCallbacks(execute=True):
            credit.submit()
        einvoice = EInvoice.objects.get(sales_invoice=credit)
        self.assertEqual(einvoice.invoice_type, "standard")
        root = etree.fromstring(einvoice.xml.encode())
        ns = {"cbc": NS["cbc"], "cac": NS["cac"]}
        self.assertEqual(root.findtext("cbc:InvoiceTypeCode", namespaces=ns), "381")
        self.assertEqual(root.findtext("cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID", namespaces=ns),
                         original.name)
        self.assertTrue(root.findtext("cac:PaymentMeans/cbc:InstructionNote", namespaces=ns))
        self.assertEqual(root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=ns), "115.00")

    def test_debit_note_type_383_with_reference_and_reason(self):
        self.onboard()
        original = self.sale(customer=self.b2b_customer(), qty=2)
        note = original.make_debit_note(self.user)
        self.assertTrue(note.name.startswith("SDN-"))
        note.items.create(item=self.item, qty=1, rate=40)
        note.calculate_totals()
        note.save()
        with self.assertRaisesMessage(ValidationError, "سبب الإشعار المدين"):
            note.submit()
        note.remarks += " فرق سعر بعد تعديل العقد"
        note.save()
        with self.captureOnCommitCallbacks(execute=True):
            note.submit()
        root = etree.fromstring(EInvoice.objects.get(sales_invoice=note).xml.encode())
        ns = {"cbc": NS["cbc"], "cac": NS["cac"]}
        self.assertEqual(root.findtext("cbc:InvoiceTypeCode", namespaces=ns), "383")
        self.assertEqual(root.findtext("cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID", namespaces=ns),
                         original.name)
        self.assertIn("فرق سعر", root.findtext("cac:PaymentMeans/cbc:InstructionNote", namespaces=ns))
        self.assertEqual(root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=ns), "46.00")

    def test_zero_rated_export_carries_category_and_exemption_reason(self):
        self.onboard()
        self.item.vat_category, self.item.vat_exemption_reason = "Z", "VATEX-SA-32"
        self.item.save()
        invoice = self.sale(customer=self.b2b_customer(), qty=2)
        self.assertEqual((invoice.vat_category, invoice.tax_amount, invoice.grand_total), ("Z", 0, Decimal("200.00")))
        einvoice = EInvoice.objects.get(sales_invoice=invoice)
        root = etree.fromstring(einvoice.xml.encode())
        ns = {"cbc": NS["cbc"], "cac": NS["cac"]}
        category = root.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", namespaces=ns)
        self.assertEqual(category.findtext("cbc:ID", namespaces=ns), "Z")
        self.assertEqual(category.findtext("cbc:Percent", namespaces=ns), "0.00")
        self.assertEqual(category.findtext("cbc:TaxExemptionReasonCode", namespaces=ns), "VATEX-SA-32")
        self.assertEqual(category.findtext("cbc:TaxExemptionReason", namespaces=ns), "صادرات السلع من المملكة")
        line_category = root.find("cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory", namespaces=ns)
        self.assertEqual(line_category.findtext("cbc:ID", namespaces=ns), "Z")
        self.assertIsNone(line_category.find("cbc:TaxExemptionReasonCode", namespaces=ns))
        # QR: الضريبة صفر
        self.assertEqual(decode_qr(einvoice.qr)[5].decode(), "0.00")

    def test_network_failure_keeps_invoice_posted_and_command_resubmits(self):
        self.onboard()
        self.fake.fail_network = True
        invoice = self.sale()
        self.assertTrue(invoice.is_submitted)
        self.assertEqual(invoice.einvoice.status, "failed")
        self.fake.fail_network = False
        call_command("zatca_submit", stdout=open("/dev/null", "w"))
        invoice.einvoice.refresh_from_db()
        self.assertEqual(invoice.einvoice.status, "reported")
        self.assertEqual(invoice.einvoice.attempts, 2)

    def test_rejection_and_warnings(self):
        self.onboard()
        self.fake.reject = True
        rejected = self.sale().einvoice
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("BR-KSA-01: rejected", rejected.response["messages"])
        self.fake.reject, self.fake.reporting_status = False, 202
        self.assertEqual(self.sale().einvoice.status, "warning")

    def test_inclusive_prices_with_discount_and_foreign_currency_totals(self):
        from core.models import Currency
        self.onboard()
        inclusive = self.sale(qty=3, rate=Decimal("33.33"), prices_include_tax=True, discount_amount=Decimal("10"))
        root = etree.fromstring(inclusive.einvoice.xml.encode())
        ns = {"cbc": NS["cbc"], "cac": NS["cac"]}
        lines = sum(Decimal(x) for x in root.xpath("cac:InvoiceLine/cbc:LineExtensionAmount/text()", namespaces=ns))
        total = root.find("cac:LegalMonetaryTotal", namespaces=ns)
        get = lambda tag: Decimal(total.findtext(f"cbc:{tag}", namespaces=ns))  # noqa: E731
        self.assertEqual(lines, get("LineExtensionAmount"))
        self.assertEqual(get("LineExtensionAmount") - get("AllowanceTotalAmount"), get("TaxExclusiveAmount"))
        self.assertEqual(get("TaxInclusiveAmount"), inclusive.grand_total)
        usd = self.sale(customer=self.b2b_customer(), currency=Currency.objects.get(code="USD"),
                        exchange_rate=Decimal("3.75"))
        root = etree.fromstring(usd.einvoice.xml.encode())
        sar_tax = root.xpath("cac:TaxTotal/cbc:TaxAmount[@currencyID='SAR']/text()", namespaces=ns)
        self.assertEqual(sar_tax, ["56.25"])
        self.assertEqual(root.findtext("cbc:DocumentCurrencyCode", namespaces=ns), "USD")

    def test_disabled_einvoicing_does_nothing_and_invalid_seller_blocked(self):
        invoice = self.sale()
        self.assertFalse(EInvoice.objects.filter(sales_invoice=invoice).exists())
        invoice.cancel()  # لا قيود بدون فوترة إلكترونية
        self.client.post("/einvoice/settings/", {"action": "save", "environment": "sandbox", "seller_name": "x",
                                                 "vat_number": "123", "crn": "1", "street": "s", "building_number": "1",
                                                 "district": "d", "city": "c", "postal_code": "1", "branch_name": "b",
                                                 "industry": "i", "invoice_types": "1100"})
        response = self.client.post("/einvoice/settings/", {"action": "compliance", "otp": "123345"}, follow=True)
        self.assertContains(response, "الرقم الضريبي للبائع")
        self.assertEqual(self.fake.calls, [])

    def test_list_page_xml_download_and_isolation(self):
        self.onboard()
        invoice = self.sale()
        self.assertContains(self.client.get("/einvoice/"), invoice.name)
        xml = self.client.get(f"/einvoice/{invoice.einvoice.pk}/xml/")
        self.assertEqual(xml["Content-Type"], "application/xml; charset=utf-8")
        other, other_user = make_company("شركة أخرى", "other_zatca")
        self.client.force_login(other_user)
        self.assertNotContains(self.client.get("/einvoice/"), invoice.name)
        self.assertEqual(self.client.get(f"/einvoice/{invoice.einvoice.pk}/xml/").status_code, 404)

    def test_regular_user_cannot_open_settings(self):
        user = User.objects.create_user("clerk", "", "pass12345")
        Membership.objects.create(user=user, company=self.company, role=Role.USER)
        self.client.force_login(user)
        self.assertRedirects(self.client.get("/einvoice/settings/"), "/")
        self.assertEqual(self.client.get("/einvoice/").status_code, 200)
