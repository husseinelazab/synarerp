from django.test import TestCase, override_settings

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from website.models import HeroSlide, ImpactCounter, Website
from website.site import clear_cache
from website.templatetags.site import bank_line

HOST = "albirr.org.sa"


class BankLineTests(TestCase):
    def test_splits_bank_and_groups_iban(self):
        line = bank_line("مصرف الراجحي - SA0380000000608010167519")
        self.assertEqual(line["bank"], "مصرف الراجحي")
        self.assertEqual(line["iban_raw"], "SA0380000000608010167519")
        self.assertEqual(line["iban"], "SA03 8000 0000 6080 1016 7519")
        self.assertEqual(bank_line("حساب بدون آيبان")["iban"], "")


@override_settings(ALLOWED_HOSTS=["*"])
class HomeDesignTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, _ = make_nonprofit("جمعية البر الأهلية", "design_owner")
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="albirr", domain=HOST, is_published=True,
                                              bank_accounts="البنك الأهلي - SA4410000000000000000000",
                                              whatsapp="966500000000")
            HeroSlide.objects.create(title="اكفل يتيماً اليوم", button_text="اكفل الآن", button_url="donate/", sort=1)
            HeroSlide.objects.create(title="شريحة مخفية", is_published=False)
            ImpactCounter.objects.create(label="أسرة مكفولة", value=320, suffix="+", icon="family")

    def setUp(self):
        clear_cache()

    def test_home_layout(self):
        page = self.client.get("/", HTTP_HOST=HOST)
        self.assertContains(page, "اكفل يتيماً اليوم")
        self.assertContains(page, 'href="/donate/">اكفل الآن')
        self.assertNotContains(page, "شريحة مخفية")
        self.assertContains(page, 'data-count="320"')
        self.assertContains(page, "أسرة مكفولة")
        self.assertContains(page, "الخدمات الإلكترونية")
        self.assertContains(page, 'data-copy="SA4410000000000000000000"')
        self.assertContains(page, "wa-float")
        # المتجر غير مفعّل: لا يظهر صندوق التبرع السريع
        self.assertNotContains(page, "data-quick-donate")

    def test_menu_links_resolve_on_domain(self):
        page = self.client.get("/about/", HTTP_HOST=HOST)
        for path in ("/governance/#financials", "/videos/", "/verify/", "/feedback/", "/services/", "/privacy/",
                     "/newsletter/"):
            self.assertContains(page, path)

    def test_without_counters_uses_live_numbers_without_zeros(self):
        with tenant_context(self.company):
            ImpactCounter.objects.all().delete()
        page = self.client.get("/", HTTP_HOST=HOST)
        self.assertNotContains(page, 'data-count="0"')
