from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from saas.tests import make_company
from website.models import Website
from website.site import clear_cache


class WebsiteFoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الموقع", "site_owner")
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="albirr", domain="albirr.org.sa", tagline="معاً للخير",
                                              fundraising_license_number="FR-123")

    def setUp(self):
        clear_cache()

    def test_preview_requires_membership_until_published(self):
        self.assertEqual(self.client.get("/site/albirr/").status_code, 404)
        self.client.force_login(self.user)
        page = self.client.get("/site/albirr/")
        self.assertContains(page, "جمعية الموقع")
        self.assertContains(page, "معاينة")
        self.assertContains(page, 'href="/site/albirr/donate/"')  # الروابط ببادئة المعاينة
        self.client.logout()
        outsider = User.objects.create_user("outsider_site", password="x")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get("/site/albirr/").status_code, 404)

    @override_settings(ALLOWED_HOSTS=["testserver", "albirr.org.sa", "www.albirr.org.sa"])
    def test_independent_domain_serves_site_at_root(self):
        self.assertNotContains(self.client.get("/", HTTP_HOST="albirr.org.sa"), "معاً للخير")  # غير منشور: صفحة المنصة
        self.site.is_published = True
        self.site.save()
        clear_cache()
        page = self.client.get("/", HTTP_HOST="www.albirr.org.sa")
        self.assertContains(page, "معاً للخير")
        self.assertContains(page, 'href="/donate/"')
        self.assertContains(page, "ترخيص جمع التبرعات رقم (<bdi>FR-123</bdi>)")
        self.assertNotContains(page, "معاينة — الموقع غير منشور")
        self.assertEqual(self.client.get("/reports/accounts/trial-balance/", HTTP_HOST="albirr.org.sa").status_code, 404)

    @override_settings(ALLOWED_HOSTS=["*"], DYNAMIC_SITE_HOSTS=True, PLATFORM_HOSTS={"testserver", "app.example.com"})
    def test_dynamic_hosts_accept_registered_domains_only(self):
        self.site.is_published = True
        self.site.save()
        clear_cache()
        self.assertEqual(self.client.get("/", HTTP_HOST="albirr.org.sa").status_code, 200)
        self.assertEqual(self.client.get("/", HTTP_HOST="evil.example.org").status_code, 400)
        self.assertEqual(self.client.get("/login/", HTTP_HOST="app.example.com").status_code, 200)

    def test_settings_page_and_validation(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/website/settings/").status_code, 200)
        response = self.client.post("/website/settings/", {"slug": "albirr", "domain": "https://www.Albirr.org.sa/",
                                                           "primary_color": "#123456", "is_published": "on"})
        self.assertEqual(response.status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual((self.site.domain, self.site.is_published), ("albirr.org.sa", True))
        response = self.client.post("/website/settings/", {"slug": "Bad Slug", "primary_color": "#123456"})
        self.assertContains(response, "أحرف إنجليزية صغيرة")

    def test_business_company_has_no_site(self):
        company, user = make_company("منشأة بموقع", "biz_site")
        with tenant_context(company):
            Website.objects.create(slug="bizsite")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/site/bizsite/").status_code, 404)

    def test_tls_domain_check_only_for_published_sites(self):
        self.assertEqual(self.client.get("/site-domain-check/", {"domain": "albirr.org.sa"}).status_code, 404)
        self.site.is_published = True
        self.site.save()
        clear_cache()
        self.assertEqual(self.client.get("/site-domain-check/", {"domain": "www.albirr.org.sa"}).status_code, 200)
        self.assertEqual(self.client.get("/site-domain-check/", {"domain": "other.org.sa"}).status_code, 404)
