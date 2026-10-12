import datetime

from django.core.management import call_command
from django.test import TestCase

from core.models import Notification
from core.tenancy import tenant_context
from crm.models import Activity, Lead, Opportunity
from saas.tests import make_company
from selling.models import Customer, Quotation


class CrmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة CRM", "crm")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.lead = Lead.objects.create(name="فهد", organization="مؤسسة النخبة", phone="0500000000", source="website")

    def test_lead_to_opportunity_to_quotation_to_won(self):
        self.client.force_login(self.user)
        response = self.client.post(f"/app/lead/{self.lead.pk}/make_opportunity/")
        opp = Opportunity.objects.get()
        self.assertRedirects(response, f"/app/opportunity/{opp.pk}/edit/", fetch_redirect_response=False)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "qualified")
        self.client.post(f"/app/opportunity/{opp.pk}/make_quotation/")
        opp.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "converted")
        self.assertEqual(Customer.objects.get(name="مؤسسة النخبة"), opp.customer)
        self.assertEqual((opp.stage, opp.quotation), ("proposal", Quotation.objects.get()))
        self.client.post(f"/app/opportunity/{opp.pk}/mark_won/")
        opp.refresh_from_db()
        self.assertEqual((opp.stage, opp.probability), ("won", 100))
        self.assertIsNotNone(opp.closed_on)

    def test_pipeline_and_followups(self):
        Opportunity.objects.create(title="توريد أجهزة", lead=self.lead, expected_amount=100000, probability=40,
                                   stage="proposal")
        Activity.objects.create(lead=self.lead, summary="اتصال متابعة", assigned_to=self.user,
                                date=datetime.date.today() - datetime.timedelta(days=1))
        self.client.force_login(self.user)
        page = self.client.get("/crm/pipeline/")
        self.assertEqual(page.context["forecast"], 40000)
        self.assertContains(page, "توريد أجهزة")
        follow = self.client.get("/crm/followups/")
        self.assertEqual(len(follow.context["overdue"]), 1)
        call_command("crm_reminders", stdout=open("/dev/null", "w"))
        call_command("crm_reminders", stdout=open("/dev/null", "w"))
        self.assertEqual(Notification.objects.filter(user=self.user, title__contains="متابعة مستحقة").count(), 1)
        self.assertContains(self.client.get(f"/app/lead/{self.lead.pk}/"), "اتصال متابعة")
        for slug in ("lead", "opportunity", "activity"):
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200)
