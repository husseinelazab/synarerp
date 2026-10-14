from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


class OnboardingTests(TestCase):
    """قائمة خطوات البداية في لوحة التحكم للمنشأة الجديدة."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("منشأة جديدة", "onb_owner")

    def test_new_company_sees_steps_marked_automatically(self):
        self.client.force_login(self.owner)
        page = self.client.get("/")
        self.assertContains(page, "ابدأ تجهيز منشأتك")
        steps = {s["title"]: s["done"] for s in page.context["onboarding"]["steps"]}
        self.assertFalse(steps["العملاء"])
        self.assertFalse(steps["بيانات المنشأة"])
        with tenant_context(self.company):
            Customer.objects.create(name="أول عميل")
        self.company.tax_id, self.company.address = "310122393500003", "الرياض"
        self.company.save()
        steps = {s["title"]: s["done"] for s in self.client.get("/").context["onboarding"]["steps"]}
        self.assertTrue(steps["العملاء"])
        self.assertTrue(steps["بيانات المنشأة"])

    def test_nonprofit_has_its_own_steps(self):
        self.company.organization_type = "nonprofit"
        self.company.save(update_fields=["organization_type"])
        self.client.force_login(self.owner)
        page = self.client.get("/")
        self.assertContains(page, "ابدأ تجهيز الجمعية")
        titles = [s["title"] for s in page.context["onboarding"]["steps"]]
        self.assertIn("أول مستفيد", titles)
        self.assertNotIn("الربط مع الفوترة الإلكترونية", titles)

    def test_dismiss_hides_for_everyone_and_only_managers_can(self):
        user = User.objects.create_user("onb_user", password="x")
        Membership.objects.create(user=user, company=self.company, role=Role.USER)
        self.client.force_login(user)
        self.client.post("/settings/onboarding/dismiss/")
        self.company.refresh_from_db()
        self.assertFalse(self.company.onboarding_dismissed)

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/settings/onboarding/dismiss/").status_code, 405)
        self.assertRedirects(self.client.post("/settings/onboarding/dismiss/"), "/")
        self.company.refresh_from_db()
        self.assertTrue(self.company.onboarding_dismissed)
        self.assertNotContains(self.client.get("/"), "ابدأ تجهيز")


class HiddenModulesTests(TestCase):
    """إخفاء الوحدات غير المستخدمة من القائمة الجانبية من إعدادات الشركة."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("جمعية القائمة", "hide_owner")
        cls.company.organization_type = "nonprofit"
        cls.company.save(update_fields=["organization_type"])

    @staticmethod
    def form_data(form):
        """بيانات النموذج الحالية كما يرسلها المتصفح (الخانات غير المحددة لا تُرسل)."""
        data = {name: form[name].value() for name in form.fields}
        return {k: ("" if v is None else v) for k, v in data.items() if v is not False}

    def menu_keys(self):
        return [m["key"] for m in self.client.get("/").context["nav_modules"]]

    def test_hidden_modules_leave_the_menu(self):
        self.client.force_login(self.owner)
        self.assertIn("awqaf", self.menu_keys())
        page = self.client.get("/settings/company/")
        self.assertContains(page, 'value="awqaf"')
        self.assertNotContains(page, 'name="hidden_modules" value="accounts"')  # المحاسبة لا تُخفى
        data = self.form_data(page.context["form"])
        data["hidden_modules"] = ["awqaf", "fleet"]
        response = self.client.post("/settings/company/", data)
        self.assertRedirects(response, "/settings/company/")
        self.company.refresh_from_db()
        self.assertEqual(sorted(self.company.hidden_modules), ["awqaf", "fleet"])
        keys = self.menu_keys()
        self.assertNotIn("awqaf", keys)
        self.assertNotIn("fleet", keys)
        self.assertIn("accounts", keys)

    def test_unknown_module_rejected(self):
        self.client.force_login(self.owner)
        data = self.form_data(self.client.get("/settings/company/").context["form"])
        data["hidden_modules"] = ["accounts"]
        self.assertIn("hidden_modules", self.client.post("/settings/company/", data).context["form"].errors)


class AdvancedFieldsTests(TestCase):
    """الحقول النادرة في الفواتير تُطوى في «خيارات إضافية»."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة النماذج", "adv_owner")

    def test_invoice_form_folds_rare_fields(self):
        self.client.force_login(self.owner)
        page = self.client.get("/app/sales-invoice/new/")
        names = page.context["advanced_names"]
        for name in ("price_list", "coupon_code", "exchange_rate", "tax_rate"):
            self.assertIn(name, names)
        for name in ("customer", "posting_date", "discount_amount"):
            self.assertNotIn(name, names)
        self.assertContains(page, '<details class="form-advanced">')
        self.assertContains(page, 'name="price_list"')  # موجود في النموذج وإن كان مطوياً

    def test_opens_when_editing_with_non_default_values(self):
        from core.tenancy import tenant_context
        from selling.models import Customer, SalesInvoice

        self.client.force_login(self.owner)
        with tenant_context(self.company):
            customer = Customer.objects.create(name="عميل")
            plain = SalesInvoice.objects.create(customer=customer)
            custom = SalesInvoice.objects.create(customer=customer, coupon_code="SAVE10")
        self.assertFalse(self.client.get(f"/app/sales-invoice/{plain.pk}/edit/").context["advanced_open"])
        self.assertTrue(self.client.get(f"/app/sales-invoice/{custom.pk}/edit/").context["advanced_open"])

    def test_nonprofit_keeps_fund_visible(self):
        self.company.organization_type = "nonprofit"
        self.company.save(update_fields=["organization_type"])
        self.client.force_login(self.owner)
        page = self.client.get("/app/purchase-invoice/new/")
        self.assertNotIn("fund", page.context["advanced_names"])
        self.assertIn("price_list", page.context["advanced_names"])
