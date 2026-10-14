import json
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Fund
from awqaf.models import Waqf
from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from funds.models import DonationReceipt, Donor
from store import public
from store.models import (DonationOpportunity, DonationOrder, DonorLogin, Marketer, OrderStatus, RecurringDonation,
                          StoreSettings)
from website.models import Website
from website.site import clear_cache

BASE = "/site/albirr/donate/"


def paid_payment(order, payment_id="pay_123", status="paid", amount=None, token=None):
    source = {"type": "creditcard", "company": "mada", "number": "4201-32XX-XXXX-1234"}
    if token:
        source["token"] = token
    return {"id": payment_id, "status": status, "amount": order.amount_halalas if amount is None else amount,
            "currency": "SAR", "metadata": {"order": order.number, "ref": order.reference}, "source": source}


@override_settings(SYNAR_MESSAGING_ASYNC=False)
class StoreTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية المتجر", "store_owner")
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="albirr", is_published=True, fundraising_license_number="FR-1",
                                              bank_accounts="الراجحي - SA0000000000000000000001")
            cls.settings = StoreSettings.for_company(cls.company)
            cls.settings.enabled = True
            cls.settings.publishable_key = "pk_test_abc"
            cls.settings.secret_key = "sk_test_secret"
            cls.settings.webhook_secret = "hook-secret"
            cls.settings.save()
            cls.general = DonationOpportunity.objects.create(title="السقيا العامة", kind="general",
                                                             target_amount=10000)
            cls.zakat_fund = Fund.objects.get(name="صندوق الزكاة")
            cls.zakat = DonationOpportunity(title="زكاة المال", kind="zakat")
            cls.zakat.full_clean()
            cls.zakat.save()
            cls.waqf = Waqf.objects.create(name="وقف البئر")
            cls.share = DonationOpportunity(title="سهم وقف البئر", kind="waqf", waqf=cls.waqf, fixed_amount=100)
            cls.share.full_clean()
            cls.share.save()
            cls.marketer = Marketer.objects.create(name="سفير الخير", code="safeer")

    def setUp(self):
        clear_cache()
        patcher = mock.patch("messaging.service.send", return_value=(None, ""))
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, opportunity, now=False, **data):
        data.setdefault("website", "")
        if now:
            data["now"] = "1"
        return self.client.post(f"{BASE}{opportunity.pk}/", data)

    def checkout(self, **extra):
        data = {"donor_name": "محمد", "donor_phone": "0501234567", "website": ""}
        data.update(extra)
        response = self.client.post(f"{BASE}checkout/", data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and
                         response.context.get("form") and response.context["form"].errors)
        return DonationOrder.all_objects.filter(company=self.company).latest("id")

    def callback(self, payment):
        with mock.patch("store.moyasar.fetch_payment", return_value=payment) as fetch:
            response = self.client.get(f"{BASE}callback/", {"id": payment["id"], "status": "paid"})
        return response, fetch


class GateTests(StoreTestBase):
    def test_home_lists_opportunities(self):
        page = self.client.get(BASE)
        self.assertContains(page, "السقيا العامة")
        self.assertContains(page, "سهم بـ 100")
        self.assertNotContains(page, "التبرع الإلكتروني غير متاح")

    def test_unlicensed_site_shows_bank_accounts_and_blocks_checkout(self):
        self.site.fundraising_license_number = ""
        self.site.save()
        page = self.client.get(BASE)
        self.assertContains(page, "التبرع الإلكتروني غير متاح")
        self.assertContains(page, "SA0000000000000000000001")
        self.add(self.general, amount="100")
        self.assertContains(self.client.get(f"{BASE}checkout/"), "التبرع الإلكتروني غير متاح")

    def test_store_not_ready_blocks_checkout(self):
        StoreSettings.all_objects.filter(company=self.company).update(enabled=False)
        self.assertContains(self.client.get(BASE), "التبرع الإلكتروني غير متاح")
        self.assertContains(self.client.get(f"{BASE}checkout/"), "التبرع الإلكتروني غير متاح")

    def test_featured_opportunities_for_home_page(self):
        with tenant_context(self.company):
            items = public.featured_opportunities(limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["url_name"], "store_opportunity")
        self.assertEqual(set(items[0]), {"pk", "title", "url_name", "url_args", "image", "raised", "target",
                                         "percent", "kind_label"})


class FlowTests(StoreTestBase):
    def test_full_flow_creates_submitted_receipts_once(self):
        self.client.get(f"{BASE}?ref=safeer")
        self.add(self.general, amount="250")
        self.add(self.share, quantity="3")
        cart = self.client.get(f"{BASE}cart/")
        self.assertContains(cart, "السقيا العامة")
        order = self.checkout(gift_to_name="أمي", gift_to_phone="0559998888", gift_message="تقبل الله")
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(order.total, Decimal("550"))
        self.assertEqual(order.marketer, self.marketer)
        self.assertEqual(order.donor_phone, "966501234567")
        pay = self.client.get(f"{BASE}pay/{order.number}/")
        self.assertContains(pay, "moyasar.js")
        self.assertContains(pay, "pk_test_abc")
        self.assertContains(pay, '"amount": 55000')

        response, fetch = self.callback(paid_payment(order))
        fetch.assert_called_once_with("sk_test_secret", "pay_123")
        self.assertRedirects(response, f"{BASE}thanks/{order.number}/", fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assertEqual(order.payment_method, "مدى")
        receipts = DonationReceipt.all_objects.filter(company=self.company, reference_no="pay_123")
        self.assertEqual(receipts.count(), 2)
        self.assertTrue(all(r.docstatus == 1 for r in receipts))
        self.assertTrue(all(r.account_id == self.settings.clearing_account_id for r in receipts))
        self.assertTrue(all(r.payment_method == "online" for r in receipts))
        general = receipts.get(amount=250)
        self.assertEqual((general.donation_type, general.fund_id), ("general", None))
        endowment = receipts.get(amount=300)
        self.assertEqual((endowment.donation_type, endowment.fund_id), ("endowment", self.waqf.principal_fund_id))
        donor = Donor.all_objects.get(company=self.company, phone="966501234567")
        self.assertEqual(order.donor, donor)
        self.assertEqual(general.donor, donor)
        # رسالة شكر للمتبرع ورسالة إهداء
        phones = [c.args[1] for c in self.send.call_args_list]
        self.assertIn("966501234567", phones)
        self.assertIn("966559998888", phones)

        thanks = self.client.get(f"{BASE}thanks/{order.number}/")
        self.assertContains(thanks, "تم استلام تبرعك بنجاح")
        self.assertContains(self.client.get(f"{BASE}certificate/{order.number}/"), "شهادة شكر وتقدير")
        # الإعادة لا تكرر السندات
        self.callback(paid_payment(order))
        self.assertEqual(DonationReceipt.all_objects.filter(company=self.company, reference_no="pay_123").count(), 2)
        self.assertEqual(DonationOpportunity.all_objects.get(pk=self.general.pk).raised, Decimal("250"))
        # جلسة أخرى لا ترى التفاصيل، والشهادة برمز التحقق فقط
        self.client.logout()
        self.client.cookies.clear()
        self.assertNotContains(self.client.get(f"{BASE}thanks/{order.number}/"), "محمد")
        self.assertEqual(self.client.get(f"{BASE}certificate/{order.number}/").status_code, 404)
        self.assertEqual(self.client.get(f"{BASE}certificate/{order.number}/?c={order.verify_code}").status_code,
                         200)

    def test_mismatched_amount_or_failed_status_leaves_unpaid(self):
        self.add(self.general, amount="100")
        order = self.checkout()
        self.callback(paid_payment(order, amount=100))  # ريال واحد بدل 100
        order.refresh_from_db()
        self.assertNotEqual(order.status, OrderStatus.PAID)
        self.callback(paid_payment(order, payment_id="pay_f", status="failed"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.FAILED)
        self.assertFalse(DonationReceipt.all_objects.filter(company=self.company).exists())
        # ثم دفع صحيح بعد الفشل
        self.callback(paid_payment(order, payment_id="pay_ok"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)

    def test_zakat_routes_to_zakat_fund_and_anonymous(self):
        self.add(self.zakat, now=True, amount="500")
        order = self.checkout(donor_name="", is_anonymous="on")
        self.callback(paid_payment(order, payment_id="pay_z"))
        receipt = DonationReceipt.all_objects.get(company=self.company, reference_no="pay_z")
        self.assertEqual((receipt.donation_type, receipt.fund), ("zakat", self.zakat_fund))
        self.assertEqual(receipt.donor_name, "فاعل خير")

    def test_honeypot_and_validation(self):
        self.add(self.general, amount="100")
        response = self.client.post(f"{BASE}checkout/", {"donor_name": "", "donor_phone": "12", "website": ""})
        self.assertContains(response, "أدخل رقم جوال صحيحاً")
        self.client.post(f"{BASE}checkout/", {"donor_name": "بوت", "donor_phone": "0501234567", "website": "x"})
        self.assertFalse(DonationOrder.all_objects.filter(company=self.company).exists())
        self.assertContains(self.add(self.general, amount="0.5"), "المبلغ من")


class WebhookTests(StoreTestBase):
    def post(self, payload):
        return self.client.post("/store/webhook/", json.dumps(payload), content_type="application/json")

    def make_order(self):
        self.add(self.general, amount="100")
        return self.checkout()

    def test_wrong_secret_forbidden_right_secret_finalizes_idempotently(self):
        order = self.make_order()
        payment = paid_payment(order, payment_id="pay_w")
        body = {"type": "payment_paid", "secret_token": "wrong", "data": payment}
        self.assertEqual(self.post(body).status_code, 403)
        self.assertEqual(self.post({"type": "payment_paid"}).status_code, 400)
        body["secret_token"] = "hook-secret"
        with mock.patch("store.moyasar.fetch_payment", return_value=payment) as fetch:
            response = self.post(body)
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with("sk_test_secret", "pay_w")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.callback(payment)  # عودة المتبرع بعد الويب هوك
        with mock.patch("store.moyasar.fetch_payment", return_value=payment):
            self.post(body)
        self.assertEqual(DonationReceipt.all_objects.filter(company=self.company, reference_no="pay_w").count(), 1)

    def test_webhook_does_not_trust_payload(self):
        order = self.make_order()
        payment = paid_payment(order, payment_id="pay_x")
        with mock.patch("store.moyasar.fetch_payment", return_value=dict(payment, status="initiated")):
            self.post({"type": "payment_paid", "secret_token": "hook-secret", "data": payment})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)


class AccountAndRecurringTests(StoreTestBase):
    def test_otp_login_lists_orders_and_cancels_recurring(self):
        self.add(self.general, amount="100")
        order = self.checkout(is_monthly="on")
        self.callback(paid_payment(order, payment_id="pay_m", token="token_abc"))
        rd = RecurringDonation.all_objects.get(company=self.company)
        self.assertEqual(rd.token, "token_abc")
        self.assertNotEqual(rd.token_encrypted, "token_abc")
        self.client.cookies.clear()

        with mock.patch("store.site_views.secrets.randbelow", return_value=123456):
            self.client.post(f"{BASE}account/", {"action": "send", "phone": "0501234567", "website": ""})
        self.assertTrue(self.send.called)
        self.assertIn("123456", self.send.call_args.args[2])
        login = DonorLogin.all_objects.get(company=self.company)
        self.assertNotIn("123456", login.code_hash)
        # إعادة الإرسال خلال دقيقة مرفوضة
        resend = self.client.post(f"{BASE}account/", {"action": "send", "phone": "0501234567", "website": ""})
        self.assertContains(resend, "انتظر دقيقة")
        wrong = self.client.post(f"{BASE}account/", {"action": "verify", "code": "000000", "website": ""})
        self.assertContains(wrong, "رمز التحقق غير صحيح")
        self.client.post(f"{BASE}account/", {"action": "verify", "code": "123456", "website": ""})
        page = self.client.get(f"{BASE}account/")
        self.assertContains(page, order.number)
        self.assertContains(page, "التبرعات الشهرية")
        self.client.post(f"{BASE}account/", {"action": "cancel_recurring", "id": rd.pk, "website": ""})
        rd.refresh_from_db()
        self.assertFalse(rd.active)

    def test_otp_attempts_limited(self):
        self.add(self.general, amount="100")
        self.checkout()
        self.client.cookies.clear()
        with mock.patch("store.site_views.secrets.randbelow", return_value=111111):
            self.client.post(f"{BASE}account/", {"action": "send", "phone": "0501234567", "website": ""})
        for _ in range(5):
            self.client.post(f"{BASE}account/", {"action": "verify", "code": "999999", "website": ""})
        response = self.client.post(f"{BASE}account/", {"action": "verify", "code": "111111", "website": ""})
        self.assertContains(response, "تجاوزت عدد المحاولات")

    def test_recurring_command_charges_due_donation(self):
        with tenant_context(self.company):
            rd = RecurringDonation(donor_name="سارة", donor_phone="966501112222", opportunity=self.general,
                                   amount=Decimal("50"), next_charge_on=timezone.localdate())
            rd.token = "token_r"
            rd.save()

        def charge(secret, payload):
            self.assertEqual(payload["source"], {"type": "token", "token": "token_r"})
            self.assertEqual(payload["amount"], 5000)
            return {"id": "pay_r", "status": "paid", "amount": payload["amount"], "currency": "SAR",
                    "metadata": payload["metadata"], "source": {"type": "token"}}

        with mock.patch("store.moyasar.create_payment", side_effect=charge):
            call_command("store_recurring", stdout=mock.MagicMock())
        rd.refresh_from_db()
        self.assertEqual(rd.last_order.status, OrderStatus.PAID)
        self.assertGreater(rd.next_charge_on, timezone.localdate())
        self.assertEqual(DonationReceipt.all_objects.get(company=self.company, reference_no="pay_r").docstatus, 1)

    def test_recurring_failures_notify_and_retry(self):
        with tenant_context(self.company):
            rd = RecurringDonation(donor_name="سارة", donor_phone="966501112222", opportunity=self.general,
                                   amount=Decimal("50"), next_charge_on=timezone.localdate())
            rd.token = "token_r"
            rd.save()
        failed = {"id": "pay_f", "status": "failed", "source": {"message": "INSUFFICIENT_FUNDS"}}
        from store.service import charge_due_recurring

        with mock.patch("store.moyasar.create_payment", return_value=failed), \
                mock.patch("core.notify.notify_managers") as notify:
            charge_due_recurring()
            charge_due_recurring(timezone.localdate() + timezone.timedelta(days=1))
        rd.refresh_from_db()
        self.assertEqual(rd.failures, 2)
        self.assertTrue(rd.active)
        notify.assert_called_once()


class ErpPagesTests(StoreTestBase):
    def test_dashboard_settings_and_doctypes(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/store/").status_code, 200)
        page = self.client.get("/store/settings/")
        self.assertContains(page, "/store/webhook/")
        self.assertNotContains(page, "sk_test_secret")
        response = self.client.post("/store/settings/", {
            "enabled": "on", "mode": "test", "publishable_key": "pk_test_new", "secret_key": "",
            "webhook_secret": "", "clearing_account": self.settings.clearing_account_id,
            "suggested_amounts": "10,20", "thank_you_message": "شكراً", "allow_recurring": "on"})
        self.assertEqual(response.status_code, 302)
        saved = StoreSettings.all_objects.get(company=self.company)
        self.assertEqual((saved.publishable_key, saved.secret_key), ("pk_test_new", "sk_test_secret"))
        bad = self.client.post("/store/settings/", {
            "enabled": "on", "mode": "live", "publishable_key": "pk_test_new",
            "clearing_account": self.settings.clearing_account_id, "suggested_amounts": "10"})
        self.assertContains(bad, "pk_live_")
        for slug in ("donation-opportunity", "donation-order", "marketer", "recurring-donation"):
            self.assertEqual(self.client.get(f"/app/{slug}/").status_code, 200, slug)
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200, slug)
        self.assertContains(self.client.get(f"/app/donation-opportunity/{self.general.pk}/"), "التحصيل")
        self.assertContains(self.client.get(f"/app/marketer/{self.marketer.pk}/"), "?ref=safeer")
        response = self.client.post("/app/donation-opportunity/new/", {
            "title": "كفالة بلا صندوق", "kind": "restricted", "sort_order": 0, "is_published": "on"})
        self.assertContains(response, "حدد الصندوق")
        self.assertEqual(self.client.get(f"/store/opportunity/{self.general.pk}/image/").status_code, 200)

    def test_order_detail_shows_lines(self):
        self.add(self.general, amount="100")
        order = self.checkout()
        self.callback(paid_payment(order, payment_id="pay_d"))
        self.client.force_login(self.user)
        page = self.client.get(f"/app/donation-order/{order.pk}/")
        self.assertContains(page, "pay_d")
        self.assertContains(page, "بنود الطلب")
