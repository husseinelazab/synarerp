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

    def test_report_letterhead_for_print(self):
        from core.models import PrintSettings

        self.company.tax_id, self.company.address = "310000000000003", "طريق الملك فهد، الرياض"
        self.company.save()
        PrintSettings.objects.create(company=self.company, logo="data:image/png;base64,AAAA", name_en="Layout Co")
        self.client.force_login(self.owner)
        html = self.client.get("/reports/accounts/trial-balance/").content.decode()
        head = html.split('class="letterhead')[1].split("</header>")[0]
        for text in ("شركة التخطيط", "Layout Co", "310000000000003", "طريق الملك فهد", "data:image/png", "عن الفترة من"):
            self.assertIn(text, head)
        self.assertIn('class="signatures"', html)  # التقارير المالية بخانات توقيع، والقوائم العادية بدونها
        self.assertNotIn('class="signatures"', self.client.get("/app/customer/").content.decode())


class ListAndTopbarTests(TestCase):
    """أداء القوائم مع كثرة السجلات، ونص البحث العلوي حسب نوع المنشأة."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة القوائم", "list_owner")

    def item_list_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get("/app/item/").status_code, 200)
        return len(queries)

    def test_list_queries_do_not_grow_with_rows(self):
        from stock.models import UOM, Item, ItemGroup

        self.client.force_login(self.owner)
        with tenant_context(self.company):
            group, uom = ItemGroup.objects.first(), UOM.objects.first()
            Item.objects.create(code="L0", name="صنف 0", item_group=group, uom=uom)
            self.item_list_queries()  # الطلب الأول يحمّل الجلسة والإعدادات
            few = self.item_list_queries()
            for n in range(1, 15):
                Item.objects.create(code=f"L{n}", name=f"صنف {n}", item_group=group, uom=uom)
        self.assertEqual(self.item_list_queries(), few)

    def test_search_placeholder_matches_organization_type(self):
        self.client.force_login(self.owner)
        page = self.client.get("/")
        self.assertContains(page, "ابحث عن عميل، صنف، فاتورة")
        self.assertNotContains(page, "ابحث عن مستفيد")
        self.company.organization_type = "nonprofit"
        self.company.save(update_fields=["organization_type"])
        self.assertContains(self.client.get("/"), "ابحث عن مستفيد، متبرع")
