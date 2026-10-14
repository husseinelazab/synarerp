from django.core.cache import cache
from django.test import TestCase

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from office.models import Correspondence
from website.models import Website
from website.ratelimit import LIMIT_MESSAGE
from website.site import clear_cache


class RateLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الحد", "rl_owner")
        with tenant_context(cls.company):
            Website.objects.create(slug="rlsite", is_published=True)

    def setUp(self):
        cache.clear()
        clear_cache()

    def test_contact_form_is_limited_per_device(self):
        data = {"name": "زائر", "phone": "0551234567", "email": "", "subject": "استفسار", "message": "مرحباً"}
        for _ in range(5):
            self.client.post("/site/rlsite/contact/", data, REMOTE_ADDR="10.0.0.1")
        response = self.client.post("/site/rlsite/contact/", data, REMOTE_ADDR="10.0.0.1")
        self.assertContains(response, LIMIT_MESSAGE)
        self.client.post("/site/rlsite/contact/", data, REMOTE_ADDR="10.0.0.2")  # جهاز آخر غير متأثر
        with tenant_context(self.company):
            self.assertEqual(Correspondence.objects.count(), 6)
