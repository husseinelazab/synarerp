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
        # رقم جوال أو مستند مطابق تماماً يفتح السجل مباشرة
        self.assertRedirects(self.client.get("/search/", {"q": "0550001122"}), self.customer.get_absolute_url(),
                             fetch_redirect_response=False)
        self.assertContains(self.client.get("/search/", {"q": "0550001122", "all": "1"}), "النخبة")
        self.assertContains(self.client.get("/search/", {"q": "ن"}), "حرفين على الأقل")

    def test_ignores_hamza_diacritics_and_word_order(self):
        with tenant_context(self.company):
            Customer.objects.create(name="شَرِكَةُ أحمد الإبراهيمي", phone="٠٥٥٩٨٧")
        self.client.force_login(self.owner)
        for query in ("شركه احمد", "الابراهيمي احمد", "شركة إبراهيمي", "شَركة", "5598"):
            self.assertContains(self.client.get("/search/", {"q": query}), "الإبراهيمي", msg_prefix=query)
        self.assertNotContains(self.client.get("/search/", {"q": "احمد سالم"}), "الإبراهيمي")
        self.assertContains(self.client.get("/search/", {"q": "موسسه"}), "النخبة")  # مؤسسة
        self.assertContains(self.client.get("/app/customer/", {"q": "مؤسسه النخبه"}), "النخبة")

    def test_special_characters_are_literal(self):
        self.client.force_login(self.owner)
        for query in ("(.*", "a+b", "[x", "\\d"):
            self.assertEqual(self.client.get("/search/", {"q": query}).status_code, 200)
        self.assertNotContains(self.client.get("/search/", {"q": ".*"}), "النخبة")

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

    def test_pages_and_create_actions(self):
        self.client.force_login(self.owner)
        page = self.client.get("/search/", {"q": "ميزان المراجعة"})
        self.assertContains(page, "الصفحات والتقارير")
        self.assertContains(page, "/reports/accounts/trial-balance/")
        page = self.client.get("/search/", {"q": "العملاء"})  # الجمع يطابق اسم الصفحة، مع اختصار الإنشاء
        self.assertContains(page, "/app/customer/new/")

    def test_suggest_returns_pages_exact_and_records(self):
        self.client.force_login(self.owner)
        data = self.client.get("/search/suggest/", {"q": "النخبة"}).json()
        titles = {g["title"]: g["items"] for g in data["groups"]}
        self.assertIn(self.customer.get_absolute_url(), [i["url"] for i in titles["العملاء"]])
        self.assertNotIn("المنافسة", str(data))
        data = self.client.get("/search/suggest/", {"q": "0550001122"}).json()
        self.assertEqual(data["groups"][0]["title"], "مطابقة تامة")
        data = self.client.get("/search/suggest/", {"q": "ميزان"}).json()
        self.assertIn("الصفحات", [g["title"] for g in data["groups"]])
        self.assertEqual(self.client.get("/search/suggest/", {"q": "ن"}).json(), {"groups": []})

    def test_suggest_respects_permissions(self):
        user = User.objects.create_user("suggest_user", password="x")
        Membership.objects.create(user=user, company=self.company, role=Role.USER, denied_doctypes=["customer"])
        self.client.force_login(user)
        self.assertNotIn(self.customer.get_absolute_url(), str(self.client.get("/search/suggest/", {"q": "النخبة"}).json()))

    def test_singular_matches_plural(self):
        from core.search import matches, stem

        self.assertTrue(matches("الكفالات", "كفالة"))
        self.assertTrue(matches("ملفات المستفيدين", "المستفيدون"))
        self.assertEqual(stem("في"), "في")
        with tenant_context(self.company):
            Customer.objects.create(name="جمعية الكفالات الخيرية")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/search/", {"q": "كفالة"}), "جمعية الكفالات الخيرية")


class PageLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة التخطيط", "layout_owner")

    def test_heading_and_breadcrumb_in_page(self):
        self.client.force_login(self.owner)
        with tenant_context(self.company):
            customer = Customer.objects.create(name="عميل المسار")
        page = self.client.get(customer.get_absolute_url())
        html = page.content.decode()
        self.assertIn('class="page-head"', html)
        self.assertIn('class="crumbs"', html)
        self.assertIn('href="/app/customer/"', html.split('class="crumbs"')[1].split("</nav>")[0])
        self.assertIn("عميل المسار</h1>", html)
        self.assertNotIn("topbar-title", html)
        self.assertIn('id="nav-filter"', html)

    def test_small_modules_have_flat_sections(self):
        from core.navigation import FLAT_LIMIT

        self.client.force_login(self.owner)
        modules = self.client.get("/").context["nav_modules"]
        for module in modules:
            items = sum(len(s["items"]) for s in module["sections"])
            self.assertEqual(module["flat"], items <= FLAT_LIMIT, module["label"])
