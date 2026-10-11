
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.models import CostCenter
from core import totp
from core.models import Membership, Role, UserTwoFactor
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


class PermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الصلاحيات", "permowner")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.clerk = User.objects.create_user("clerk", password="Clerk#Pass123")
        self.member = Membership.objects.create(user=self.clerk, company=self.company, role=Role.USER)
        self.client.force_login(self.clerk)
        self.customer = Customer.objects.create(name="عميل الفروع")
        self.item = Item.objects.create(code="P1", name="خدمة", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), is_stock_item=False, selling_rate=100)
        self.main = CostCenter.objects.get(name="الفرع الرئيسي")
        self.jeddah = CostCenter.objects.create(name="فرع جدة", parent=self.main.parent)

    def invoice(self, center, submit=True):
        inv = SalesInvoice.objects.create(customer=self.customer, update_stock=False, cost_center=center)
        inv.items.create(item=self.item, qty=1, rate=100)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
        return inv

    def test_module_restriction(self):
        self.member.modules = ["selling"]
        self.member.save()
        self.assertEqual(self.client.get(reverse("doc_list", args=["sales-invoice"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("doc_list", args=["payment-entry"])).status_code, 200)  # مشترك
        self.assertEqual(self.client.get(reverse("party_balances", args=["customer"])).status_code, 200)
        self.assertRedirects(self.client.get(reverse("party_balances", args=["supplier"])), reverse("dashboard"),
                             fetch_redirect_response=False)
        for url in (reverse("doc_list", args=["purchase-invoice"]), reverse("trial_balance"),
                    reverse("doc_list", args=["employee"]), reverse("asset_register"), reverse("stock_balance")):
            self.assertRedirects(self.client.get(url), reverse("dashboard"), fetch_redirect_response=False)
        page = self.client.get(reverse("dashboard"))
        self.assertTemplateUsed(page, "core/dashboard_limited.html")
        menu_keys = [m["key"] for m in page.context["nav_modules"]]
        self.assertIn("selling", menu_keys)
        self.assertNotIn("accounts", menu_keys)
        self.assertNotIn("hr", menu_keys)

    def test_empty_modules_means_all_and_managers_unrestricted(self):
        self.assertEqual(self.client.get(reverse("trial_balance")).status_code, 200)
        self.assertTemplateUsed(self.client.get(reverse("dashboard")), "core/dashboard.html")

    def test_draft_only_user(self):
        self.member.can_submit = False
        self.member.save()
        inv = self.invoice(self.main, submit=False)
        self.client.post(reverse("doc_action", args=["sales-invoice", inv.pk, "submit"]))
        inv.refresh_from_db()
        self.assertTrue(inv.is_draft)
        response = self.client.post(reverse("doc_edit", args=["sales-invoice", inv.pk]), {
            "customer": self.customer.pk, "posting_date": str(inv.posting_date), "tax_rate": "15", "vat_category": "S",
            "discount_amount": "0", "cost_center": self.main.pk, "_submit": "1",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": inv.items.first().pk, "items-0-item": self.item.pk, "items-0-qty": "1", "items-0-rate": "100"})
        self.assertContains(response, "المسودات فقط")
        inv.refresh_from_db()
        self.assertTrue(inv.is_draft)
        # المدير يعتمد
        self.client.force_login(self.owner)
        self.client.post(reverse("doc_action", args=["sales-invoice", inv.pk, "submit"]))
        inv.refresh_from_db()
        self.assertTrue(inv.is_submitted)

    def test_branch_restriction(self):
        mine, other = self.invoice(self.jeddah), self.invoice(self.main)
        self.member.cost_centers.set([self.jeddah])
        listing = self.client.get(reverse("doc_list", args=["sales-invoice"]))
        self.assertContains(listing, mine.name)
        self.assertNotContains(listing, other.name)
        self.assertEqual(self.client.get(reverse("doc_detail", args=["sales-invoice", other.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("doc_print", args=["sales-invoice", other.pk])).status_code, 404)
        form = self.client.get(reverse("doc_new", args=["sales-invoice"]))
        self.assertEqual(list(form.context["form"].fields["cost_center"].queryset), [self.jeddah])
        self.assertEqual(form.context["form"].initial["cost_center"], self.jeddah.pk)

    def test_manager_edits_permissions(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("user_edit", args=[self.member.pk]), {
            "role": "user", "modules": ["selling", "stock"], "cost_centers": [self.jeddah.pk]})
        self.member.refresh_from_db()
        self.assertEqual((self.member.modules, self.member.can_submit), (["selling", "stock"], False))
        self.assertEqual(list(self.member.cost_centers.all()), [self.jeddah])
        self.assertContains(self.client.get(reverse("users_list")), "مسودات فقط")


class TwoFactorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الأمان", "secureboss")
        cls.user.set_password("Owner#Pass123")
        cls.user.save()

    def setUp(self):
        cache.clear()

    def login(self, password="Owner#Pass123"):
        return self.client.post(reverse("login"), {"username": "secureboss", "password": password})

    def enable(self):
        self.client.force_login(self.user)
        self.client.get(reverse("two_factor_setup"))
        secret = self.client.session["2fa_pending_secret"]
        self.client.post(reverse("two_factor_setup"), {"action": "enable",
                                                       "code": totp.code_at(secret, totp.current_step())})
        self.client.logout()
        return secret

    def test_rfc_vectors(self):
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
        self.assertEqual(totp.code_at(secret, 59 // 30), "287082")
        self.assertEqual(totp.code_at(secret, 1111111109 // 30), "081804")

    def test_enable_shows_recovery_codes_and_secret_encrypted(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("two_factor_setup"))
        self.assertContains(page, "<svg")
        secret = self.client.session["2fa_pending_secret"]
        self.client.post(reverse("two_factor_setup"), {"action": "enable", "code": "000000"})
        self.assertFalse(UserTwoFactor.objects.filter(enabled=True).exists())
        response = self.client.post(reverse("two_factor_setup"), {
            "action": "enable", "code": totp.code_at(secret, totp.current_step())}, follow=True)
        record = UserTwoFactor.objects.get(user=self.user)
        self.assertTrue(record.enabled)
        self.assertNotIn(secret, record.secret_encrypted)
        self.assertEqual(len(record.recovery_codes), 10)
        self.assertEqual(len(response.context["recovery"]), 10)

    def test_login_requires_code(self):
        secret = self.enable()
        response = self.login()
        self.assertRedirects(response, reverse("login_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(self.client.post(reverse("login_verify"), {"code": "111111"}), "غير صحيح")
        record = UserTwoFactor.objects.get()
        # الرمز السابق صالح ضمن نافذة ±30 ثانية، لكنه استُخدم عند التفعيل فلا يُقبل مرة أخرى
        step = max(totp.current_step(), record.last_step + 1)
        response = self.client.post(reverse("login_verify"), {"code": totp.code_at(secret, step)})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_recovery_code_single_use(self):
        self.client.force_login(self.user)
        self.client.get(reverse("two_factor_setup"))
        secret = self.client.session["2fa_pending_secret"]
        response = self.client.post(reverse("two_factor_setup"), {
            "action": "enable", "code": totp.code_at(secret, totp.current_step())}, follow=True)
        code = response.context["recovery"][0]
        self.client.logout()
        self.login()
        self.client.post(reverse("login_verify"), {"code": code})
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(len(UserTwoFactor.objects.get().recovery_codes), 9)
        self.client.logout()
        self.login()
        self.assertContains(self.client.post(reverse("login_verify"), {"code": code}), "غير صحيح")

    def test_too_many_wrong_codes_resets(self):
        self.enable()
        self.login()
        for _ in range(5):
            response = self.client.post(reverse("login_verify"), {"code": "123456"})
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("login_verify")), reverse("login"),
                             fetch_redirect_response=False)

    def test_password_lockout(self):
        for _ in range(5):
            self.login(password="wrong")
        response = self.login()  # حتى الصحيحة تُرفض أثناء الإيقاف
        self.assertContains(response, "محاولات دخول خاطئة كثيرة")
        self.assertNotIn("_auth_user_id", self.client.session)
        cache.clear()
        self.assertRedirects(self.login(), reverse("dashboard"), fetch_redirect_response=False)

    def test_company_can_require_two_factor(self):
        self.company.require_two_factor = True
        self.company.save()
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("two_factor_setup"),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
        self.client.logout()
        self.enable()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_disable_needs_password_and_code(self):
        from unittest import mock

        now = 1_800_000_000
        with mock.patch("core.totp.time.time", return_value=now):
            secret = self.enable()
        self.client.force_login(self.user)
        with mock.patch("core.totp.time.time", return_value=now + 60):
            code = totp.code_at(secret, totp.current_step(now + 60))
            self.client.post(reverse("two_factor_setup"), {"action": "disable", "password": "wrong", "code": code})
            self.assertTrue(UserTwoFactor.objects.filter(enabled=True).exists())
            self.client.post(reverse("two_factor_setup"), {"action": "disable", "password": "Owner#Pass123",
                                                           "code": code})
        self.assertFalse(UserTwoFactor.objects.exists())


class HardeningTests(TestCase):
    def test_client_ip_ignores_spoofed_forwarded_for(self):
        from core.utils import client_ip

        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.5")
        with override_settings(SYNAR_TRUST_PROXY=False):
            self.assertEqual(client_ip(request), "10.0.0.5")
        # خلف الوكيل: آخر عنوان هو ما أضافه الوكيل، وما قبله من العميل
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9", REMOTE_ADDR="172.18.0.2")
        with override_settings(SYNAR_TRUST_PROXY=True):
            self.assertEqual(client_ip(request), "203.0.113.9")

    def test_ip_lockout_not_bypassed_by_forwarded_for(self):
        cache.clear()
        make_company("شركة القفل", "lockowner")
        for i in range(20):
            self.client.post(reverse("login"), {"username": f"guess{i}", "password": "bad"})
        response = self.client.post(reverse("login"), {"username": "lockowner", "password": "pass12345"},
                                    HTTP_X_FORWARDED_FOR="1.2.3.4")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["locked"])
        cache.clear()

    def test_export_does_not_produce_formulas(self):
        import io

        import openpyxl

        from core.importer import build_csv, columns_for
        from core.registry import REGISTRY

        company, owner = make_company("شركة التصدير", "exportowner")
        with tenant_context(company):
            Customer.objects.create(name='=HYPERLINK("http://evil.example","x")')
        self.client.force_login(owner)
        response = self.client.get(reverse("doc_export", args=["customer"]))
        sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
        cells = [c for row in sheet.iter_rows() for c in row if str(c.value or "").startswith("=")]
        self.assertTrue(cells)
        self.assertTrue(all(c.data_type == "s" for c in cells))
        csv_text = build_csv(columns_for(REGISTRY["customer"]), [["=1+1", "-2", "عادي"]]).decode("utf-8-sig")
        self.assertIn("'=1+1,'-2,عادي", csv_text)
