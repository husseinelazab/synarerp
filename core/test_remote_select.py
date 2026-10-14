import re
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from core.models import Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


@mock.patch("core.remote_select.THRESHOLD", 5)
class RemoteSelectTests(TestCase):
    """القوائم الكبيرة تُعرض بالخيار المختار فقط وتُبحث على الخادم ضمن الشركة والمستخدم."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة القوائم الكبيرة", "remote_owner")
        cls.other, cls.other_owner = make_company("شركة أخرى", "remote_other")
        with tenant_context(cls.company):
            Customer.objects.bulk_create([Customer(company=cls.company, name=f"عميل رقم {n}") for n in range(40)])
            cls.target = Customer.objects.create(name="مؤسسة النخبة للتجارة", phone="0551112233")
        with tenant_context(cls.other):
            Customer.objects.bulk_create([Customer(company=cls.other, name=f"النخبة المنافسة {n}") for n in range(10)])

    def setUp(self):
        cache.clear()
        from core import remote_select

        remote_select._counts.clear()

    def remote_url(self, html, field="customer"):
        match = re.search(r'<select name="%s"[^>]*data-remote="([^"]+)"' % field, html) or \
            re.search(r'<select[^>]*data-remote="([^"]+)"[^>]*name="%s"' % field, html)
        self.assertIsNotNone(match, "حقل العميل لم يتحول إلى بحث من الخادم")
        return match.group(1)

    def test_form_renders_only_selected_option_and_searches_server(self):
        self.client.force_login(self.owner)
        html = self.client.get("/app/sales-invoice/new/").content.decode()
        self.assertNotIn("عميل رقم 7", html)
        url = self.remote_url(html)
        data = self.client.get(url, {"q": "النخبه"}).json()  # البحث يتجاهل التاء المربوطة
        self.assertEqual([r["id"] for r in data["results"]], [self.target.pk])
        self.assertEqual(self.client.get(url, {"q": "0551112233"}).json()["results"][0]["id"], self.target.pk)
        everyone = self.client.get(url).json()
        self.assertTrue(everyone["more"])
        self.assertFalse(any("المنافسة" in r["text"] for r in everyone["results"]))  # لا تسرب من شركة أخرى

    def test_selected_value_kept_when_editing(self):
        self.client.force_login(self.owner)
        html = self.client.get("/app/sales-invoice/new/", {"customer": self.target.pk}).content.decode()
        self.assertIn(f'<option value="{self.target.pk}" selected>مؤسسة النخبة للتجارة</option>', html)

    def test_token_bound_to_user_and_company(self):
        self.client.force_login(self.owner)
        url = self.remote_url(self.client.get("/app/sales-invoice/new/").content.decode())
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(url).status_code, 404)
        stranger = User.objects.create_user("remote_member", password="x")
        Membership.objects.create(user=stranger, company=self.company, role=Role.USER)
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.get("/api/options/unknown/").status_code, 302)

    def test_small_lists_stay_local(self):
        self.client.force_login(self.other_owner)
        with mock.patch("core.remote_select.THRESHOLD", 300):
            html = self.client.get("/app/sales-invoice/new/").content.decode()
        self.assertNotIn("data-remote", html)
        self.assertIn("النخبة المنافسة 3", html)
