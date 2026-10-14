import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail, signing
from django.core.management import call_command
from django.core.mail import get_connection
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Company, EmailSettings, Membership, Role
from core.pdf import LINK_SALT, share_token
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import Item


def locmem(*args, **kwargs):
    return get_connection("django.core.mail.backends.locmem.EmailBackend")


@override_settings(SYNAR_EMAIL_ASYNC=False, SYNAR_MESSAGING_ASYNC=False)
@mock.patch("core.mail.MailConfig.connection", locmem)
class DocumentPDFTests(TestCase):
    """ملفات PDF للمستندات: تنزيلها، وإرسالها بالبريد مرفقة، ورابطها العام الموقّع للعميل."""

    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("pdf_owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        with tenant_context(cls.company):
            cls.customer = Customer.objects.filter(phone__gt="").first()
            cls.customer.email = "client@example.com"
            cls.customer.save()
            cls.invoice = SalesInvoice.objects.create(customer=cls.customer, update_stock=False,
                                                      due_date=timezone.localdate() + datetime.timedelta(days=30))
            cls.invoice.items.create(item=Item.objects.get(code="PRN-001"), qty=2, rate=1000)
            cls.invoice.calculate_totals()
            cls.invoice.save()
            cls.invoice.submit()
            cls.draft = SalesInvoice.objects.create(customer=cls.customer, update_stock=False)
        cls.other, cls.other_owner = make_company("شركة أخرى", "pdf_other")

    def setUp(self):
        self.client.force_login(self.owner)

    def assertPDF(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_download_pdf_for_invoice_and_generic_document(self):
        response = self.client.get(f"/app/sales-invoice/{self.invoice.pk}/pdf/")
        self.assertPDF(response)
        self.assertIn(f'filename="{self.invoice.name}.pdf"', response["Content-Disposition"])
        self.assertGreater(len(response.content), 5000)  # فيه خطوط مضمّنة ونص فعلي
        with tenant_context(self.company):
            from accounts.models import JournalEntry

            entry = JournalEntry.objects.first()
        if entry:
            self.assertPDF(self.client.get(f"/app/journal-entry/{entry.pk}/pdf/"))

    def test_pdf_respects_company_and_permissions(self):
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(f"/app/sales-invoice/{self.invoice.pk}/pdf/").status_code, 404)
        limited = User.objects.create_user("pdf_limited", password="x")
        Membership.objects.create(user=limited, company=self.company, role=Role.USER, modules=["stock"])
        self.client.force_login(limited)
        self.assertNotEqual(self.client.get(f"/app/sales-invoice/{self.invoice.pk}/pdf/").status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(f"/app/sales-invoice/{self.invoice.pk}/pdf/").status_code, 302)

    def test_public_signed_link(self):
        page = self.client.get(f"/app/sales-invoice/{self.invoice.pk}/")
        self.assertContains(page, "نسخ رابط للعميل")
        token = share_token(self.company, "sales-invoice", self.invoice)
        self.client.logout()
        response = self.client.get(f"/d/{token}/")
        self.assertPDF(response)
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        # رابط معدّل أو لمستند غير قابل للمشاركة أو لمسودة يُرفض
        self.assertEqual(self.client.get(f"/d/{token[:-2]}xx/").status_code, 404)
        forged = signing.dumps({"c": self.company.pk, "s": "journal-entry", "p": 1}, salt=LINK_SALT)
        self.assertEqual(self.client.get(f"/d/{forged}/").status_code, 404)
        draft = signing.dumps({"c": self.company.pk, "s": "sales-invoice", "p": self.draft.pk}, salt=LINK_SALT)
        self.assertEqual(self.client.get(f"/d/{draft}/").status_code, 404)
        wrong_company = signing.dumps({"c": self.other.pk, "s": "sales-invoice", "p": self.invoice.pk},
                                      salt=LINK_SALT)
        self.assertEqual(self.client.get(f"/d/{wrong_company}/").status_code, 404)

    def test_email_sends_pdf_attachment(self):
        url = f"/app/sales-invoice/{self.invoice.pk}/email/"
        self.client.post(url)
        self.assertEqual(len(mail.outbox), 0)  # البريد غير مفعّل
        EmailSettings.objects.create(company=self.company, enabled=True, host="smtp.test", port=587,
                                     from_email="sales@company.test", from_name="المبيعات")
        self.assertContains(self.client.get(f"/app/sales-invoice/{self.invoice.pk}/"), "إرسال بالبريد")
        self.client.post(url)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["client@example.com"])
        self.assertIn(self.invoice.name, message.subject)
        self.assertNotIn("سِينار", message.subject)
        name, content, mimetype = message.attachments[0]
        self.assertEqual((name, mimetype), (f"{self.invoice.name}.pdf", "application/pdf"))
        self.assertTrue(content.startswith(b"%PDF-"))
        self.client.post(url, {"to": "other@example.com"})
        self.assertEqual(mail.outbox[1].to, ["other@example.com"])
        self.client.post(url, {"to": "not-an-email"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_whatsapp_message_includes_pdf_link(self):
        response = self.client.post(f"/messaging/send/invoice/{self.invoice.pk}/", {"mode": "link"})
        from urllib.parse import unquote

        text = unquote(response["Location"])
        self.assertIn("/d/", text)
        token = text.split("/d/")[1].split("/")[0]
        self.client.logout()
        self.assertPDF(self.client.get(f"/d/{token}/"))

    def test_print_page_offers_download(self):
        page = self.client.get(f"/app/sales-invoice/{self.invoice.pk}/print/")
        self.assertContains(page, f"/app/sales-invoice/{self.invoice.pk}/pdf/")
        self.assertContains(page, "fonts.googleapis.com")  # صفحة المتصفح كما هي
