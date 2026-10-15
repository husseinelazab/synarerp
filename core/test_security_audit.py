"""ثغرات وجدها فحص الأمان (الدخول، والصلاحيات، والواجهة البرمجية، وحدود المحاولات) وتبقى مغلقة."""
import json

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from core import totp
from core.models import ApiKey, ExchangeRate, Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer
from stock.models import UOM, Item, ItemGroup


class AdminLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.root = User.objects.create_superuser("root", "root@example.com", "Root#Pass123")

    def test_admin_login_goes_through_the_two_factor_login(self):
        page = self.client.get("/admin/login/?next=/admin/")
        self.assertRedirects(page, "/login/?next=/admin/", fetch_redirect_response=False)
        response = self.client.post("/admin/login/", {"username": "root", "password": "Root#Pass123",
                                                      "next": "/admin/"})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotEqual(response.status_code, 200)

    def test_admin_login_rejects_external_next(self):
        page = self.client.get("/admin/login/?next=https://evil.example/")
        self.assertIn("next=/admin/", page["Location"])


class TwoFactorGuessingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة التخمين", "guessme")
        cls.user.set_password("Owner#Pass123")
        cls.user.save()

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)
        self.client.get(reverse("two_factor_setup"))
        self.secret = self.client.session["2fa_pending_secret"]
        self.client.post(reverse("two_factor_setup"), {"action": "enable",
                                                       "code": totp.code_at(self.secret, totp.current_step())})
        self.client.logout()

    def password_step(self):
        return self.client.post(reverse("login"), {"username": "guessme", "password": "Owner#Pass123"})

    def test_correct_password_does_not_reset_wrong_code_counter(self):
        # كل جولة: كلمة مرور صحيحة ثم رموز خاطئة؛ الإيقاف يقع رغم صحة كلمة المرور في كل مرة
        for _ in range(3):
            if self.password_step().status_code == 200:
                break
            for _ in range(5):
                self.client.post(reverse("login_verify"), {"code": "000000"})
        page = self.password_step()
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["locked"])

    def test_correct_code_clears_the_counter(self):
        self.password_step()
        self.client.post(reverse("login_verify"), {"code": "000000"})
        # الخطوة التالية: رمز التفعيل نفسه مستخدم ولا يُقبل مرتين
        self.client.post(reverse("login_verify"), {"code": totp.code_at(self.secret, totp.current_step() + 1)})
        self.assertIn("_auth_user_id", self.client.session)


class ApiFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الواجهة", "apifilter")
        cls.other, cls.other_owner = make_company("شركة الضحية", "victim")

    def setUp(self):
        cache.clear()
        _, self.raw = ApiKey.issue(self.company, self.owner, "فحص")

    def get(self, path, raw=None):
        return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def test_relation_paths_are_ignored(self):
        with tenant_context(self.company):
            Customer.objects.create(name="عميل")
            total = self.get("/api/v1/customer/").json()["count"]
        for query in ("currency__company__name__in=شركة الضحية", "currency__company__memberships__user__username__in=x",
                      "created_by__password__gte=~~~~"):
            self.assertEqual(self.get(f"/api/v1/customer/?{query}").json()["count"], total, query)
        self.assertEqual(self.get("/api/v1/customer/?name__in=لا أحد").json()["count"], 0)


class ApiPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الصلاحيات", "permowner")
        cls.clerk = User.objects.create_user("crmclerk", password="x")
        Membership.objects.create(user=cls.clerk, company=cls.company, role=Role.USER, modules=["crm", "stock"],
                                  can_submit=False, can_delete=False, hide_costs=True)

    def setUp(self):
        cache.clear()
        _, self.raw = ApiKey.issue(self.company, self.clerk, "محدود")
        with tenant_context(self.company):
            self.item = Item.objects.create(code="C1", name="صنف", item_group=ItemGroup.objects.first(),
                                            uom=UOM.objects.first(), selling_rate=10, buying_rate=7)

    def call(self, method, path, data=None):
        return getattr(self.client, method)(path, data=json.dumps(data) if data else None,
                                            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}")

    def test_manager_pages_and_actions(self):
        self.assertEqual(self.call("get", "/api/v1/approval-rule/").status_code, 403)
        self.assertEqual(self.call("get", "/api/v1/fiscal-year/").status_code, 403)
        self.assertEqual(self.call("delete", f"/api/v1/item/{self.item.pk}/").status_code, 403)
        self.assertTrue(Item.all_objects.filter(pk=self.item.pk).exists())

    def test_costs_hidden(self):
        data = self.call("get", f"/api/v1/item/{self.item.pk}/").json()
        self.assertNotIn("buying_rate", data)
        self.assertEqual(data["selling_rate"], "10.00")
        self.assertEqual(self.call("get", "/api/v1/item/?buying_rate__gte=5").json()["count"],
                         self.call("get", "/api/v1/item/").json()["count"])


class WebPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الإعدادات", "setupowner")
        cls.clerk = User.objects.create_user("crmonly", password="x")
        Membership.objects.create(user=cls.clerk, company=cls.company, role=Role.USER, modules=["crm"],
                                  can_submit=False, hide_costs=True)
        with tenant_context(cls.company):
            cls.item = Item.objects.create(code="C2", name="صنف", item_group=ItemGroup.objects.first(),
                                           uom=UOM.objects.first(), selling_rate=10, buying_rate=7)

    def setUp(self):
        self.client.force_login(self.clerk)

    def test_setup_records_closed_to_other_modules(self):
        for path in ("/app/fiscal-year/", "/app/exchange-rate/new/", "/app/recurring-document/"):
            self.assertEqual(self.client.get(path).status_code, 302, path)

    def test_item_json_hides_cost(self):
        data = self.client.get(f"/api/item/{self.item.pk}/").json()
        self.assertNotIn("buying_rate", data)
        self.assertTrue(all("buying_rate" not in unit for unit in data["uoms"].values()))

    def test_auto_submit_needs_submit_permission(self):
        from core.models import RecurringDocument

        with tenant_context(self.company):
            rec = RecurringDocument(company=self.company, title="س", doctype="journal-entry", source_id=1,
                                    next_date="2026-01-01", submit_automatically=True)
            from django.core.exceptions import ValidationError

            from core.tenancy import _current_user, set_current_user

            for user, allowed in ((self.clerk, False), (self.owner, True)):
                token = set_current_user(user)
                try:
                    if allowed:
                        rec.full_clean()
                    else:
                        with self.assertRaises(ValidationError):
                            rec.full_clean()
                finally:
                    _current_user.reset(token)


class PublicThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_password_reset_is_rate_limited(self):
        from django.core import mail

        User.objects.create_user("resetme", "resetme@example.com", "x")
        from core import password_reset

        if not password_reset.reset_available():
            self.skipTest("استعادة كلمة المرور غير مفعلة في هذه البيئة")
        for _ in range(8):
            self.client.post(reverse("password_reset"), {"email": "resetme@example.com"})
        self.assertLessEqual(len(mail.outbox), 5)


@override_settings(DEBUG=False)
class ErrorPageTests(TestCase):
    def test_arabic_404(self):
        page = self.client.get("/no-such-page-at-all/")
        self.assertEqual(page.status_code, 404)
        self.assertContains(page, "الصفحة غير موجودة", status_code=404)
