import datetime

from django.test import TestCase

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from relations.models import Survey
from volunteering.models import VolunteerOpportunity
from website.models import Website
from website.site import clear_cache


class EmbeddedPublicFormsTests(TestCase):
    """نماذج الوحدات العامة تُعرض داخل إطار موقع الجمعية متى فُتحت من الموقع."""

    @classmethod
    def setUpTestData(cls):
        cls.company, _ = make_nonprofit("جمعية البر الأهلية", "embed_owner")
        today = datetime.date.today()
        with tenant_context(cls.company):
            Website.objects.create(slug="albirr", is_published=True)
            cls.survey = Survey.objects.create(title="رضا المستفيدين")
            cls.opportunity = VolunteerOpportunity.objects.create(
                title="توزيع السلال", start_date=today, end_date=today + datetime.timedelta(days=5), status="open")

    def setUp(self):
        clear_cache()

    def test_survey_and_volunteer_forms_stay_inside_site(self):
        for url in (f"/site/albirr/s/{self.survey.public_token}/", f"/site/albirr/v/{self.opportunity.public_token}/"):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertContains(page, "site-header")
            self.assertContains(page, "embedded")
            self.assertContains(page, "page-hero")

    def test_platform_links_keep_standalone_page(self):
        page = self.client.get(f"/s/{self.survey.public_token}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "site-header")
