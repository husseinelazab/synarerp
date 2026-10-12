import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from core.models import Company, Membership, Role
from core.provisioning import provision_company
from core.tenancy import tenant_context
from saas.models import Plan, SubscriptionStatus
from saas.services import add_months, ensure_default_plans, record_payment
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse


def make_company(name, username, plan_code="pro", role=Role.OWNER):
    ensure_default_plans()
    user = User.objects.create_user(username, f"{username}@example.com", "pass12345")
    company = provision_company(name, plan=Plan.objects.get(code=plan_code))
    Membership.objects.create(user=user, company=company, role=role)
    return company, user


class TenantIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a, cls.user_a = make_company("شركة أ", "owner_a")
        cls.b, cls.user_b = make_company("شركة ب", "owner_b")
        for company in (cls.a, cls.b):
            with tenant_context(company):
                customer = Customer.objects.create(name="عميل مشترك الاسم")  # نفس الاسم مسموح في شركتين
                item = Item.objects.create(code="X1", name=f"صنف {company.name}", item_group=ItemGroup.objects.first(),
                                           uom=UOM.objects.first(), is_stock_item=False, selling_rate=100)
                inv = SalesInvoice.objects.create(customer=customer, warehouse=Warehouse.objects.first())
                inv.items.create(item=item, qty=1, rate=100)
                inv.submit()
        cls.customer_a = Customer.all_objects.get(company=cls.a, name="عميل مشترك الاسم")
        cls.invoice_a = SalesInvoice.all_objects.get(company=cls.a)
        cls.invoice_b = SalesInvoice.all_objects.get(company=cls.b)

    def test_document_numbers_are_per_company(self):
        self.assertEqual(self.invoice_a.name, self.invoice_b.name)

    def test_lists_and_detail_are_isolated(self):
        self.client.force_login(self.user_b)
        response = self.client.get("/app/item/")
        self.assertContains(response, "صنف شركة ب")
        self.assertNotContains(response, "صنف شركة أ")
        self.assertEqual(self.client.get(f"/app/customer/{self.customer_a.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/app/sales-invoice/{self.invoice_a.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/app/sales-invoice/{self.invoice_a.pk}/print/").status_code, 404)

    def test_form_dropdowns_only_show_own_records(self):
        self.client.force_login(self.user_b)
        form = self.client.get("/app/sales-invoice/new/").context["form"]
        customer_ids = set(form.fields["customer"].queryset.values_list("pk", flat=True))
        self.assertNotIn(self.customer_a.pk, customer_ids)
        self.assertEqual(customer_ids, set(Customer.all_objects.filter(company=self.b).values_list("pk", flat=True)))

    def test_cannot_post_other_company_record(self):
        self.client.force_login(self.user_b)
        response = self.client.post("/app/sales-invoice/new/", {
            "customer": self.customer_a.pk, "posting_date": "2026-05-01", "tax_rate": "15", "discount_amount": "0",
            "items-TOTAL_FORMS": "0", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 200)  # يُرفض النموذج: العميل غير موجود ضمن خيارات الشركة
        self.assertFalse(SalesInvoice.all_objects.filter(company=self.b, customer=self.customer_a).exists())

    def test_cancel_does_not_touch_other_company_with_same_number(self):
        with tenant_context(self.b):
            SalesInvoice.objects.get(pk=self.invoice_b.pk).cancel()
        self.assertFalse(GLEntry.all_objects.filter(company=self.a, is_cancelled=True).exists())
        self.assertTrue(GLEntry.all_objects.filter(company=self.b, is_cancelled=True).exists())

    def test_reports_are_isolated(self):
        self.client.force_login(self.user_a)
        response = self.client.get("/reports/accounts/trial-balance/")
        totals = response.context["totals"]
        own = GLEntry.all_objects.filter(company=self.a, is_cancelled=False).aggregate(d=Sum("debit"))["d"]
        self.assertEqual(totals[2], own)

    def test_pos_items_api_isolated(self):
        self.client.force_login(self.user_a)
        self.client.post("/pos/open/", {"profile": self.a_profile().pk, "opening_cash": "0"})
        names = [i["name"] for i in self.client.get("/pos/api/items/").json()["items"]]
        self.assertIn("صنف شركة أ", names)
        self.assertNotIn("صنف شركة ب", names)

    def a_profile(self):
        from pos.models import POSProfile
        return POSProfile.all_objects.get(company=self.a)

    def test_cannot_open_shift_with_other_company_profile(self):
        from pos.models import POSProfile
        self.client.force_login(self.user_a)
        other = POSProfile.all_objects.get(company=self.b)
        self.assertEqual(self.client.post("/pos/open/", {"profile": other.pk}).status_code, 404)

    def test_switch_company_requires_membership(self):
        self.client.force_login(self.user_a)
        self.client.post(f"/switch-company/{self.b.pk}/")
        self.assertContains(self.client.get("/app/item/"), "صنف شركة أ")
        Membership.objects.create(user=self.user_a, company=self.b, role=Role.USER)
        self.client.post(f"/switch-company/{self.b.pk}/")
        self.assertContains(self.client.get("/app/item/"), "صنف شركة ب")


class SignupTests(TestCase):
    def test_signup_provisions_company_and_trial(self):
        ensure_default_plans()
        response = self.client.post("/signup/", {
            "company_name": "مؤسسة التجربة", "tax_id": "", "phone": "0500000000", "full_name": "سارة أحمد",
            "email": "sara@example.com", "username": "sara", "password": "Strong-Pass-123",
            "password2": "Strong-Pass-123", "plan": Plan.objects.get(code="basic").pk,
        })
        self.assertRedirects(response, "/")
        company = Company.objects.get(name="مؤسسة التجربة")
        self.assertEqual(company.subscription.status, SubscriptionStatus.TRIAL)
        self.assertEqual(company.subscription.plan.code, "basic")
        self.assertTrue(company.subscription.is_valid)
        self.assertEqual(Membership.objects.get(company=company).role, Role.OWNER)
        self.assertIsNotNone(company.default_receivable_account)
        self.assertContains(self.client.get("/"), "صافي المبيعات هذا الشهر")

    def test_signup_rejects_duplicates_and_mismatch(self):
        make_company("موجودة", "taken")
        response = self.client.post("/signup/", {
            "company_name": "موجودة", "phone": "1", "full_name": "x", "email": "taken@example.com",
            "username": "taken", "password": "Strong-Pass-123", "password2": "other", "plan": Plan.objects.first().pk,
        })
        form = response.context["form"]
        for field in ("company_name", "email", "username", "password2"):
            self.assertIn(field, form.errors)


class SubscriptionAndRoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الاشتراك", "owner", plan_code="basic")

    def add_member(self, username, role):
        user = User.objects.create_user(username, "", "pass12345")
        Membership.objects.create(user=user, company=self.company, role=role)
        return user

    def expire(self):
        sub = self.company.subscription
        sub.period_end = timezone.localdate() - datetime.timedelta(days=1)
        sub.save()

    def test_expired_subscription_blocks_usage(self):
        self.expire()
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/app/item/"), "/billing/")
        self.assertEqual(self.client.get("/billing/").status_code, 200)
        self.client.force_login(self.add_member("worker", Role.USER))
        self.assertEqual(self.client.get("/app/item/").status_code, 402)

    def test_record_payment_extends_and_upgrades(self):
        self.expire()
        pro = Plan.objects.get(code="pro")
        payment = record_payment(self.company.subscription, pro, 12, Decimal("2990"))
        sub = self.company.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.status, SubscriptionStatus.ACTIVE)
        self.assertEqual(sub.plan, pro)
        self.assertEqual(payment.period_start, timezone.localdate())
        self.assertEqual(sub.period_end, add_months(timezone.localdate(), 12) - datetime.timedelta(days=1))
        # دفعة ثانية تُضاف بعد نهاية الفترة الحالية
        record_payment(sub, pro, 1, Decimal("299"))
        sub.refresh_from_db()
        self.assertEqual(sub.period_end, add_months(payment.period_end + datetime.timedelta(days=1), 1) - datetime.timedelta(days=1))

    def test_add_months_clamps_day(self):
        self.assertEqual(add_months(datetime.date(2026, 1, 31), 1), datetime.date(2026, 2, 28))

    def test_pos_not_in_basic_plan(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/pos/"), "/billing/")

    def test_cashier_limited_to_pos(self):
        record_payment(self.company.subscription, Plan.objects.get(code="pro"), 1, Decimal("299"))
        self.client.force_login(self.add_member("cash", Role.CASHIER))
        self.assertRedirects(self.client.get("/app/customer/"), "/pos/")
        self.assertEqual(self.client.get("/pos/").status_code, 200)

    def test_regular_user_cannot_manage(self):
        self.client.force_login(self.add_member("regular", Role.USER))
        self.assertRedirects(self.client.get("/settings/users/"), "/")
        self.assertRedirects(self.client.get("/billing/"), "/")
        self.assertEqual(self.client.get("/app/item/").status_code, 200)

    def test_user_limit_enforced(self):
        self.client.force_login(self.owner)
        for i in range(2):  # الباقة الأساسية: 3 مستخدمين (المالك + 2)
            response = self.client.post("/settings/users/new/", {
                "full_name": f"موظف {i}", "username": f"emp{i}", "email": "", "password": "Strong-Pass-123", "role": "user"})
            self.assertRedirects(response, "/settings/users/")
        response = self.client.post("/settings/users/new/", {
            "full_name": "زائد", "username": "extra", "email": "", "password": "Strong-Pass-123", "role": "user"})
        self.assertContains(response, "للحد الأقصى")
        self.assertFalse(User.objects.filter(username="extra").exists())

    def test_owner_updates_company_settings(self):
        self.client.force_login(self.owner)
        form = self.client.get("/settings/company/").context["form"]
        data = {k: (v.pk if hasattr(v, "pk") else v) for k, v in form.initial.items() if v is not None}
        data.update({"name": "شركة الاشتراك المحدثة", "tax_id": "310000000000003"})
        self.assertRedirects(self.client.post("/settings/company/", data), "/settings/company/")
        self.company.refresh_from_db()
        self.assertEqual(self.company.tax_id, "310000000000003")


class PlatformTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("عميل المنصة", "client")
        cls.admin = User.objects.create_superuser("platform", "", "pass12345")

    def test_platform_requires_superuser(self):
        self.client.force_login(self.owner)
        response = self.client.get("/platform/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/login/"))

    def test_platform_dashboard_and_payment(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/platform/"), "عميل المنصة")
        response = self.client.post(f"/platform/companies/{self.company.pk}/", {
            "action": "payment", "plan": Plan.objects.get(code="enterprise").pk, "months": "12",
            "amount": "5990", "method": "bank", "reference": "TRX-1"})
        self.assertRedirects(response, f"/platform/companies/{self.company.pk}/")
        self.company.subscription.refresh_from_db()
        self.assertEqual(self.company.subscription.plan.code, "enterprise")
        self.client.post(f"/platform/companies/{self.company.pk}/", {"action": "suspend"})
        self.company.subscription.refresh_from_db()
        self.assertFalse(self.company.subscription.is_valid)

    def test_support_access_enters_company(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get("/"), "/platform/", fetch_redirect_response=False)
        self.client.post(f"/platform/companies/{self.company.pk}/", {"action": "enter"})
        self.assertEqual(self.client.get("/app/customer/").status_code, 200)


class PlanModulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الوحدات", "mod_owner", plan_code="basic")
        cls.admin = User.objects.create_superuser("plans_admin", "", "pass12345")
        cls.plan = Plan.objects.get(code="basic")

    def close(self, *keys):
        self.plan.closed_modules = list(keys)
        self.plan.save()

    def menu_keys(self):
        return [m["key"] for m in self.client.get("/").context["nav_modules"]]

    def test_closed_module_hidden_and_blocked_even_for_owner(self):
        self.close("hr", "manufacturing")
        self.client.force_login(self.owner)
        keys = self.menu_keys()
        self.assertNotIn("hr", keys)
        self.assertNotIn("manufacturing", keys)
        self.assertIn("selling", keys)
        self.assertRedirects(self.client.get("/app/employee/"), "/billing/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/app/bom/"), "/billing/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/app/customer/").status_code, 200)

    def test_shared_paths_open_while_any_module_is_open(self):
        self.close("selling")
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/app/sales-invoice/"), "/billing/", fetch_redirect_response=False)
        # سندات القبض مشتركة مع المحاسبة، والمحاسبة لا تُغلق
        self.assertEqual(self.client.get("/app/payment-entry/").status_code, 200)

    def test_closed_module_items_removed_from_other_menus(self):
        self.close("stock")
        self.client.force_login(self.owner)
        urls = [i["url"] for m in self.client.get("/").context["nav_modules"] for s in m["sections"] for i in s["items"]]
        self.assertFalse(any(u.startswith(("/app/item/", "/app/stock-entry/", "/reports/stock/balance")) for u in urls))
        self.assertTrue(any(u.startswith("/reports/stock/gross-profit/") for u in urls))  # مشترك مع المبيعات

    def test_platform_edits_plan_modules(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/platform/plans/"), "الأساسية")
        form = self.client.get(f"/platform/plans/{self.plan.pk}/").context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and k not in ("modules", "includes_ai", "is_public")}
        data["modules"] = ["selling", "buying", "stock", "pos"]
        self.assertRedirects(self.client.post(f"/platform/plans/{self.plan.pk}/", data), "/platform/plans/")
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.includes_pos)
        self.assertFalse(self.plan.is_public)
        self.assertIn("hr", self.plan.closed_modules)
        self.assertNotIn("selling", self.plan.closed_modules)
        self.assertTrue(self.plan.allows_module("accounts"))

    def test_plan_pages_require_superuser(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/platform/plans/").status_code, 302)
        self.assertTrue(self.client.get("/platform/plans/").url.startswith("/login/"))
