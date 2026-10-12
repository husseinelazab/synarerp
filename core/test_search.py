from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


class GlobalSearchTests(TestCase):
    """البحث العام في كل المستندات، والبحث في الجداول والقوائم المنسدلة."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة البحث", "search_owner")
        cls.other, _ = make_company("شركة أخرى", "search_other")
        with tenant_context(cls.company):
            cls.customer = Customer.objects.create(name="مؤسسة النخبة للتجارة", phone="0550001122")
        with tenant_context(cls.other):
            Customer.objects.create(name="مؤسسة النخبة المنافسة")

    def test_finds_across_doctypes_within_company(self):
        self.client.force_login(self.owner)
        page = self.client.get("/search/", {"q": "النخبة"})
        self.assertContains(page, self.customer.get_absolute_url())
        self.assertNotContains(page, "المنافسة")  # لا تسرب لبيانات شركة أخرى
        page = self.client.get("/search/", {"q": "البنك"})
        self.assertContains(page, "دليل الحسابات")
        self.assertContains(self.client.get("/search/", {"q": "0550001122"}), "النخبة")
        self.assertContains(self.client.get("/search/", {"q": "ن"}), "حرفين على الأقل")

    def test_respects_permissions(self):
        user = User.objects.create_user("search_user", password="x")
        Membership.objects.create(user=user, company=self.company, role=Role.USER, denied_doctypes=["customer"])
        self.client.force_login(user)
        self.assertNotContains(self.client.get("/search/", {"q": "النخبة"}), self.customer.get_absolute_url())

    def test_topbar_box_and_assets(self):
        self.client.force_login(self.owner)
        page = self.client.get("/app/customer/")
        self.assertContains(page, 'class="global-search"')
        self.assertContains(page, "js/search.js")
        self.assertContains(page, "no-search")  # قوائم المستندات لها بحثها على الخادم

    def test_account_search_lands_on_filtered_chart(self):
        self.client.force_login(self.owner)
        response = self.client.get("/app/account/", {"q": "بنك"})
        self.assertRedirects(response, "/reports/accounts/chart-of-accounts/?q=%D8%A8%D9%86%D9%83")
