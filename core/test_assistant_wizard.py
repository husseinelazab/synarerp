from unittest.mock import patch,Mock
from django.test import TestCase
from django.urls import reverse
from beneficiaries.tests import make_nonprofit
from core.models import Membership,Role,AssistantSettings,AssistantMessage,ProcessTemplate
from core.tenancy import tenant_context
from programs.models import QuranCircle
from hr.models import OrganizationUnit
from core.mail import encrypt_password


class AssistantAndWizardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_nonprofit("جمعية التهيئة والمساعد","assistant_owner")

    def setUp(self):self.client.force_login(self.owner)

    def test_local_summary_does_not_call_external_provider(self):
        with patch("core.assistant.generate") as external:
            response=self.client.post(reverse("assistant_chat"),{"topic":"work","question":"ما المطلوب مني؟"})
        self.assertEqual(response.status_code,200);self.assertContains(response,"ملخص محلي")
        external.assert_not_called()
        self.assertTrue(AssistantMessage.objects.filter(company=self.company,user=self.owner).exists())

    def test_provider_requires_separate_consent_plan_and_explicit_request(self):
        with tenant_context(self.company):
            AssistantSettings.objects.create(enabled=True,consent=True,api_key_encrypted=encrypt_password("sk-ant-fake-test"))
        subscription=self.company.subscription
        subscription.plan.includes_ai=True;subscription.plan.save(update_fields=["includes_ai"])
        with patch("core.assistant.generate",return_value="إجابة اختبار مبنية على الملخص") as generate:
            response=self.client.post(reverse("assistant_chat"),{"topic":"work","question":"فسر الملخص","use_provider":"on"})
        self.assertContains(response,"إجابة اختبار")
        facts=generate.call_args.args[2]
        self.assertNotIn("api_key",str(facts));self.assertNotIn(self.owner.username,str(facts))

    def test_forbidden_topics_and_manager_settings(self):
        Membership.objects.filter(company=self.company,user=self.owner).update(role=Role.USER,modules=["accounts"])
        response=self.client.post(reverse("assistant_chat"),{"topic":"impact"})
        self.assertFalse(response.context["form"].is_valid())
        self.assertEqual(self.client.get(reverse("assistant_settings")).status_code,302)
        Membership.objects.filter(company=self.company,user=self.owner).update(modules=["buying"])
        response=self.client.post(reverse("assistant_chat"),{"topic":"stock"})
        self.assertIn("topic",response.context["form"].errors)

    def test_setup_idempotent_preserves_members_and_creates_disabled_templates(self):
        url=reverse("setup_wizard")
        payload={"preset":"quran","confirm":"on"}
        self.assertEqual(self.client.post(url,payload).status_code,302)
        self.company.refresh_from_db();self.assertEqual(self.company.association_specialty,"quran")
        units=OrganizationUnit.all_objects.filter(company=self.company).count()
        self.client.post(url,payload)
        self.assertEqual(OrganizationUnit.all_objects.filter(company=self.company).count(),units)
        self.assertFalse(ProcessTemplate.all_objects.filter(company=self.company,enabled=True).exists())
        self.assertEqual(Membership.objects.get(company=self.company,user=self.owner).role,Role.OWNER)
        with tenant_context(self.company):QuranCircle.objects.create(name="حلقة قائمة",teacher="المعلم")
        self.client.post(url,{"preset":"health","confirm":"on"})
        self.company.refresh_from_db();self.assertEqual(self.company.association_specialty,"quran")
