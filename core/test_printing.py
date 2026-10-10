import base64
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase

from core.models import Company, Membership, PrintSettings, Role
from core.printing import trade_lines
from core.tafqeet import amount_in_words, number_to_words
from selling.models import Customer, SalesInvoice
from stock.models import Item

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class TafqeetTests(TestCase):
    def test_numbers(self):
        cases = {
            1265: "فقط ألف ومائتان وخمسة وستون ريالاً سعودياً لا غير",
            1: "فقط ريال سعودي واحد لا غير",
            2: "فقط ريالان سعوديان لا غير",
            7: "فقط سبعة ريالات سعودية لا غير",
            100: "فقط مائة ريال سعودي لا غير",
            3000: "فقط ثلاثة آلاف ريال سعودي لا غير",
            "15.03": "فقط خمسة عشر ريالاً سعودياً وثلاث هللات لا غير",
            "0.50": "فقط خمسون هللة لا غير",
        }
        for value, words in cases.items():
            with self.subTest(value=value):
                self.assertEqual(amount_in_words(Decimal(str(value))), words)
        self.assertEqual(number_to_words(2345678), "مليونان وثلاثمائة وخمسة وأربعون ألفاً وستمائة وثمانية وسبعون")
        self.assertIn("دولاراً أمريكياً", amount_in_words(99, "USD"))
        self.assertEqual(amount_in_words(-5), amount_in_words(5))


class PrintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.admin = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.admin, company=cls.company, role=Role.OWNER)
        cls.clerk = User.objects.create_user("clerk", password="pass12345")
        Membership.objects.create(user=cls.clerk, company=cls.company, role=Role.USER)

    def make_invoice(self, inclusive=False, discount=0):
        inv = SalesInvoice.objects.create(company=self.company, customer=Customer.objects.first(), update_stock=False,
                                          prices_include_tax=inclusive, discount_amount=discount)
        for item, qty in zip(Item.objects.all()[:3], (2, 1, 3)):
            inv.items.create(item=item, qty=qty, rate=item.selling_rate or 100)
        inv.calculate_totals()
        inv.save()
        return inv

    def test_line_values_add_up_to_document_totals(self):
        for inclusive in (False, True):
            with self.subTest(inclusive=inclusive):
                inv = self.make_invoice(inclusive=inclusive, discount=Decimal("101.37"))
                lines, has_discount = trade_lines(inv)
                self.assertTrue(has_discount)
                self.assertEqual(sum(ln["taxable"] for ln in lines), inv.net_total)
                self.assertEqual(sum(ln["tax"] for ln in lines), inv.tax_amount)
                self.assertEqual(sum(ln["total"] for ln in lines), inv.grand_total)

    def test_invoice_print_has_tax_invoice_essentials(self):
        inv = self.make_invoice(discount=50)
        inv.submit()
        self.client.force_login(self.admin)
        for template in ("classic", "modern", "minimal"):
            page = self.client.get(f"/app/sales-invoice/{inv.pk}/print/", {"template": template})
            self.assertContains(page, f'class="tpl-{template}"')
            self.assertContains(page, "Tax Invoice")
            self.assertContains(page, "310122393500003")
            self.assertContains(page, "فقط ")
            self.assertContains(page, "<svg")  # رمز QR
            self.assertNotContains(page, 'class="watermark"')
        # قالب غير معروف يعود للقالب المحفوظ
        self.assertContains(self.client.get(f"/app/sales-invoice/{inv.pk}/print/", {"template": "x"}), 'class="tpl-classic"')

    def test_draft_watermark_and_other_documents(self):
        inv = self.make_invoice()
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(f"/app/sales-invoice/{inv.pk}/print/"), 'class="watermark">مسودة')

    def test_settings_apply_to_print(self):
        self.client.force_login(self.admin)
        response = self.client.post("/settings/print/", {
            "template": "modern", "accent_color": "#0A7B5A", "name_en": "Test Co", "bilingual": "on",
            "show_amount_in_words": "", "show_signatures": "on", "show_item_code": "on", "show_bank_details": "on",
            "bank_name": "البنك الأهلي", "iban": "sa03 8000 0000 6080 1016 7519", "footer_text": "شكراً لكم",
            "logo_file": SimpleUploadedFile("logo.png", PNG, content_type="image/png"),
        })
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        ps = PrintSettings.objects.get(company=self.company)
        self.assertEqual((ps.template, ps.accent_color, ps.iban), ("modern", "#0a7b5a", "SA0380000000608010167519"))
        self.assertEqual(ps.logo, "data:image/png;base64," + base64.b64encode(PNG).decode())
        inv = self.make_invoice()
        inv.submit()
        page = self.client.get(f"/app/sales-invoice/{inv.pk}/print/")
        for text in ('class="tpl-modern"', "--accent: #0a7b5a", "Test Co", "SA0380000000608010167519", "شكراً لكم",
                     'src="data:image/png;base64,'):
            self.assertContains(page, text)
        self.assertNotContains(page, "فقط ")

    def test_logo_rejects_svg_and_fake_images(self):
        self.client.force_login(self.admin)
        for name, data in (("x.svg", b"<svg onload='alert(1)'></svg>"), ("x.png", b"not an image"),
                           ("big.png", PNG + b"0" * 600 * 1024)):
            with self.subTest(name=name):
                page = self.client.post("/settings/print/", {
                    "template": "classic", "accent_color": "#1f4e79",
                    "logo_file": SimpleUploadedFile(name, data)})
                self.assertEqual(page.status_code, 200)
                self.assertFalse(PrintSettings.objects.filter(company=self.company).exclude(logo="").exists())
        for bad in ("red;}</style>", "#12345g"):
            page = self.client.post("/settings/print/", {"template": "classic", "accent_color": bad})
            self.assertEqual(page.status_code, 200)
            self.assertFalse(PrintSettings.objects.filter(company=self.company).exists())

    def test_only_managers_change_print_settings(self):
        self.client.force_login(self.clerk)
        self.assertNotEqual(self.client.get("/settings/print/").status_code, 200)
        inv = self.make_invoice()
        page = self.client.get(f"/app/sales-invoice/{inv.pk}/print/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "/settings/print/")
