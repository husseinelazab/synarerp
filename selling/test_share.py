from django.test import TestCase, override_settings

from core.tenancy import tenant_context
from messaging.service import invoice_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse


class InvoiceShareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة المشاركة", "sharer")
        cls.company.tax_id = "310122393500003"                         # رمز QR يتطلب الرقم الضريبي
        cls.company.save()
        cls.other_company, cls.other_user = make_company("شركة أخرى", "othersharer")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        item = Item.objects.create(code="SH", name="خدمة", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                   is_stock_item=False)
        self.invoice = SalesInvoice.objects.create(customer=Customer.objects.create(name="عميل", phone="0501234567"),
                                                   warehouse=Warehouse.objects.first(), update_stock=False)
        self.invoice.items.create(item=item, qty=2, rate=100)

    def test_public_link_flow(self):
        self.client.force_login(self.user)
        draft = self.client.get(f"/app/sales-invoice/{self.invoice.pk}/share/")
        self.assertEqual(draft.status_code, 302)                         # المسودة لا تُشارك
        self.invoice.submit()
        page = self.client.get(f"/app/sales-invoice/{self.invoice.pk}/share/")
        url = page.context["url"]
        self.assertIn("/i/", url)
        self.assertIn("wa.me/966501234567", page.context["whatsapp"])
        self.assertIn(url, page.context["body"])
        token = url.rstrip("/").rsplit("/", 1)[1]
        self.client.logout()
        public = self.client.get(f"/i/{token}/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, self.invoice.name)
        self.assertContains(public, "<svg")                              # رمز QR
        self.assertNotContains(public, "رجوع للمستند")
        self.assertEqual(public["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.client.get("/i/wrong-token-1234567890abcdef/").status_code, 404)
        # مستخدم شركة أخرى يفتح الرابط العام أيضاً لكنه لا يصل لصفحة المشاركة الداخلية
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(f"/i/{token}/").status_code, 200)
        self.assertEqual(self.client.get(f"/app/sales-invoice/{self.invoice.pk}/share/").status_code, 404)
        # إلغاء الرابط
        self.client.force_login(self.user)
        self.client.post(f"/app/sales-invoice/{self.invoice.pk}/share/", {"action": "revoke"})
        self.assertEqual(self.client.get(f"/i/{token}/").status_code, 404)

    @override_settings(SYNAR_SITE_URL="https://erp.example.com")
    def test_messages_include_link(self):
        self.invoice.submit()
        ctx = invoice_context(self.invoice)
        self.assertTrue(ctx["link"].startswith("https://erp.example.com/i/"))
        self.assertIn(ctx["link"], ctx["link_text"])
