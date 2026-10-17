from django.test import TestCase
from django.urls import reverse
from beneficiaries.tests import make_nonprofit
from core.models import PrintSettings, Membership, Role
from core.tenancy import tenant_context
from office.models import Correspondence


class DocumentStudioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_nonprofit("جمعية استوديو المستندات","studio_owner")
        with tenant_context(cls.company):
            cls.letter=Correspondence.objects.create(direction="outgoing",subject="موضوع سري لا يظهر للتحقق",body="محتوى داخلي",party="جهة خاصة")

    def setUp(self):
        self.client.force_login(self.owner)

    def test_validated_layout_preview_and_letters(self):
        data={"margin":18,"logo_width":45,"card_width":65,"font_size":16,"line_height":"1.8","identity_side":"left","signature_side":"center"}
        self.assertEqual(self.client.post(reverse("document_studio"),data).status_code,302)
        page=self.client.get(reverse("document_studio_preview"),data)
        self.assertContains(page,"--studio-margin:18mm")
        self.assertContains(page,"identity-left signature-center")
        page=self.client.get(f"/office/documents/correspondence/{self.letter.pk}/letter/")
        self.assertContains(page,"التحقق من رقم الصادر")
        self.assertContains(page,"--studio-card:65mm")
        data["margin"]="999; color:red"
        self.assertEqual(self.client.post(reverse("document_studio"),data).status_code,200)
        self.assertEqual(PrintSettings.objects.get(company=self.company).document_layout["margin"],"18")

    def test_public_verification_is_minimal_and_confidential_letters_hidden(self):
        self.client.logout()
        url=reverse("verify_letter",args=[self.letter.verification_uuid])
        page=self.client.get(url)
        self.assertContains(page,self.letter.number);self.assertNotContains(page,self.letter.subject)
        self.assertNotContains(page,self.letter.body);self.assertNotContains(page,self.letter.party)
        Correspondence.all_objects.filter(pk=self.letter.pk).update(is_confidential=True)
        self.assertEqual(self.client.get(url).status_code,404)

    def test_only_managers_can_change_layout(self):
        Membership.objects.filter(company=self.company,user=self.owner).update(role=Role.USER)
        self.assertEqual(self.client.get(reverse("document_studio")).status_code,302)

    def test_certificate_and_reports_have_independent_layouts(self):
        data={"kind":"certificate","margin":20,"logo_width":45,"card_width":65,"font_size":18,"line_height":"1.8","identity_side":"left","signature_side":"center"}
        self.assertEqual(self.client.post(reverse("document_studio"),data).status_code,302)
        self.assertContains(self.client.get(reverse("document_studio_preview"),data),"font-size:18px")
        data["kind"]="reports";data["font_size"]=14
        self.client.post(reverse("document_studio"),data)
        self.assertContains(self.client.get(reverse("document_studio_preview"),data),"font-size:14px")
        ps=PrintSettings.objects.get(company=self.company)
        self.assertEqual(ps.document_layout["layouts"]["certificate"]["font_size"],"18")
