import datetime

from django.test import TestCase

from beneficiaries.tests import make_nonprofit
from core.forms import PrintSettingsForm
from core.hijri import format_hijri
from core.models import Attachment, PrintSettings
from core.tenancy import tenant_context
from office.models import Correspondence


class UnifiedLetterheadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_nonprofit("جمعية الخطابات", "letterhead_owner")
        cls.other, _ = make_nonprofit("جمعية أخرى", "letterhead_other")
        cls.ps = PrintSettings.objects.create(company=cls.company, accent_color="#16765c",
            logo="data:image/png;base64,b3du", letterhead="data:image/png;base64,Y3VzdG9t",
            letter_opening="السلام عليكم ورحمة الله وبركاته،")
        PrintSettings.objects.create(company=cls.other, accent_color="#912aff", logo="data:image/png;base64,b3RoZXI=")
        with tenant_context(cls.company):
            cls.letter = Correspondence.objects.create(direction="outgoing", subject="خطاب رسمي بهوية الجمعية",
                body="نص الخطاب", date=datetime.date(2026, 10, 17), party="الجهة المرسَل إليها")
            for i in range(2):
                Attachment.objects.create(doctype="correspondence", object_id=cls.letter.pk, file_name=f"{i}.pdf", content=b"test")
        with tenant_context(cls.other):
            Attachment.objects.create(doctype="correspondence", object_id=cls.letter.pk, file_name="foreign.pdf", content=b"test")

    def setUp(self):
        self.client.force_login(self.owner)
        self.url = f"/office/documents/correspondence/{self.letter.pk}/letter/"

    def test_brand_dates_actual_attachments_and_barcode_below_metadata(self):
        page = self.client.get(self.url)
        for value in ["--accent: #16765c", self.ps.logo, self.company.name, "رقم الصادر", "التاريخ الميلادي",
                      "2026/10/17", "التاريخ الهجري", format_hijri(self.letter.date), "2 ملف", self.letter.number,
                      f'aria-label="{self.letter.number}"', 'css/letter.css']:
            self.assertContains(page, value)
        for value in [self.ps.letterhead, "#912aff", "data:image/png;base64,b3RoZXI="]:
            self.assertNotContains(page, value)
        html = page.content.decode()
        self.assertGreater(html.index('class="outgoing-barcode"'), html.index("المرفقات"))
        self.assertEqual(html.count(self.ps.letter_opening), 1)
        self.assertEqual(page.context["attachments_count"], 2)

    def test_attachment_description_and_custom_image_are_explicit_choices(self):
        Correspondence.all_objects.filter(pk=self.letter.pk).update(attachment_description="كشف المستفيدين (3 صفحات)")
        self.assertContains(self.client.get(self.url), "كشف المستفيدين (3 صفحات)")
        PrintSettings.objects.filter(pk=self.ps.pk).update(letter_layout="custom")
        page = self.client.get(self.url)
        self.assertContains(page, self.ps.letterhead)
        self.assertContains(page, self.letter.number)
        self.assertNotContains(page, self.ps.logo)
        PrintSettings.objects.filter(pk=self.ps.pk).update(letterhead="")
        self.assertContains(self.client.get(self.url), self.ps.logo)

    def test_layout_form_defaults_and_rejects_unknown_layout(self):
        form = PrintSettingsForm(data={"template": "classic", "accent_color": "#16765c"}, instance=self.ps)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["letter_layout"], "unified")
        invalid = PrintSettingsForm(data={"template": "classic", "accent_color": "#16765c", "letter_layout": "injected"}, instance=self.ps)
        self.assertFalse(invalid.is_valid())
        self.assertIn("letter_layout", invalid.errors)
