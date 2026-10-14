from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from store.models import StoreSettings
from website.models import Website
from website.site import clear_cache

HOST, OTHER_HOST = "albirr.org.sa", "alkhair.org.sa"
PATH = "/.well-known/apple-developer-merchantid-domain-association"
FILE = "7B2270737049223A2241323041304245413239394436454637334635433844373846303644413041" * 20


@override_settings(ALLOWED_HOSTS=["testserver", HOST, "www." + HOST, OTHER_HOST])
class ApplePayDomainFileTests(TestCase):
    """ملف توثيق نطاق Apple Pay: يرفعه مدير الجمعية ويُعرض على نطاق موقعها في المسار الذي تطلبه Apple."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية البر", "applepay_owner")
        cls.other, _ = make_nonprofit("جمعية الخير", "applepay_other")
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="albirr", domain=HOST, is_published=True)
            cls.settings = StoreSettings.for_company(cls.company)
            cls.settings.save()
        with tenant_context(cls.other):
            Website.objects.create(slug="alkhair", domain=OTHER_HOST, is_published=True)

    def setUp(self):
        clear_cache()

    def upload(self, content, name="apple-developer-merchantid-domain-association", **extra):
        data = {"mode": "test", "publishable_key": "", "clearing_account": self.settings.clearing_account_id,
                "suggested_amounts": "50,100", "thank_you_message": "شكراً",
                "apple_pay_file": SimpleUploadedFile(name, content), **extra}
        return self.client.post("/store/settings/", data)

    def test_upload_and_serve_on_association_domain(self):
        self.assertEqual(self.client.get(PATH, HTTP_HOST=HOST).status_code, 404)
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/store/settings/"), "لم يُرفع بعد")
        self.assertRedirects(self.upload(FILE.encode() + b"\n"), "/store/settings/")
        self.client.logout()
        response = self.client.get(PATH, HTTP_HOST=HOST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertEqual(response.content.decode(), FILE)
        self.assertEqual(self.client.get(PATH, HTTP_HOST="www." + HOST).content.decode(), FILE)
        # جمعية أخرى لا ترى ملف هذه الجمعية على نطاقها
        self.assertEqual(self.client.get(PATH, HTTP_HOST=OTHER_HOST).status_code, 404)
        self.client.force_login(self.user)
        page = self.client.get("/store/settings/")
        self.assertContains(page, f"https://{HOST}{PATH}")
        # يعمل أيضاً لمستخدم مسجل الدخول يفتح الرابط على نطاق المنصة دون ملف للمنصة
        self.assertEqual(self.client.get(PATH).status_code, 404)

    def test_rejects_non_verification_content(self):
        self.client.force_login(self.user)
        for content in (b"<html><script>alert(1)</script></html>", "ملف عربي".encode(), b"\xff\xfe\x00",
                        b"A" * (40 * 1024)):
            response = self.upload(content)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors.get("apple_pay_file"))
        self.assertEqual(StoreSettings.all_objects.get(company=self.company).apple_pay_domain_file, "")

    def test_remove_file_and_keep_on_unrelated_save(self):
        self.client.force_login(self.user)
        self.upload(FILE.encode())
        data = {"mode": "test", "publishable_key": "", "clearing_account": self.settings.clearing_account_id,
                "suggested_amounts": "10", "thank_you_message": "شكراً"}
        self.client.post("/store/settings/", data)  # حفظ بلا ملف يُبقي المحفوظ
        self.assertEqual(StoreSettings.all_objects.get(company=self.company).apple_pay_domain_file, FILE)
        self.client.post("/store/settings/", {**data, "remove_apple_pay_file": "on"})
        self.assertEqual(StoreSettings.all_objects.get(company=self.company).apple_pay_domain_file, "")
        self.assertEqual(self.client.get(PATH, HTTP_HOST=HOST).status_code, 404)

    def test_platform_domain_file_from_setting(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(FILE + "\n")
        self.assertEqual(self.client.get(PATH).status_code, 404)
        with override_settings(SYNAR_APPLE_PAY_DOMAIN_FILE=fh.name):
            response = self.client.get(PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), FILE)
