import re
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import get_connection
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import EmailSettings, Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company


def locmem(*args, **kwargs):
    return get_connection("django.core.mail.backends.locmem.EmailBackend")


@override_settings(SYNAR_EMAIL_ASYNC=False)
@mock.patch("core.mail.MailConfig.connection", locmem)
class MailSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة البريد", "mailboss")
        cls.user.email = "boss@company.test"
        cls.user.save()

    def setUp(self):
        self.client.force_login(self.user)

    def save_company(self, **extra):
        data = {"host": "smtp.company.test", "port": 587, "security": "tls", "username": "hr@company.test",
                "password": "s3cret", "from_email": "hr@company.test", "from_name": "الموارد البشرية",
                "site_url": "https://erp.company.test", "action": "save"}
        data.update(extra)
        return self.client.post(reverse("company_email_settings"), data)

    def test_disabled_by_default_and_nothing_sent(self):
        from core.mail import active_config
        from core.notify import notify_managers

        self.assertIsNone(active_config(self.company))
        page = self.client.get(reverse("company_email_settings"))
        self.assertContains(page, "البريد غير مفعّل")
        notify_managers(self.company, "تنبيه")
        self.assertEqual(len(mail.outbox), 0)

    def test_fill_now_enable_later_with_encrypted_password(self):
        self.save_company()  # حفظ بدون تفعيل
        obj = EmailSettings.objects.get(company=self.company)
        self.assertFalse(obj.enabled)
        self.assertTrue(obj.password_encrypted)
        self.assertNotIn("s3cret", obj.password_encrypted)
        from core.mail import active_config, decrypt_password
        self.assertEqual(decrypt_password(obj.password_encrypted), "s3cret")
        self.assertIsNone(active_config(self.company))
        page = self.client.get(reverse("company_email_settings"))
        self.assertNotContains(page, "s3cret")
        self.assertContains(page, "محفوظة")
        # التفعيل لاحقاً دون إعادة كتابة كلمة المرور يُبقيها
        self.save_company(password="", enabled="on")
        obj.refresh_from_db()
        self.assertTrue(obj.enabled)
        self.assertEqual(decrypt_password(obj.password_encrypted), "s3cret")
        config = active_config(self.company)
        self.assertEqual((config.source, config.host, config.password), ("company", "smtp.company.test", "s3cret"))

    def test_enable_requires_host_and_sender(self):
        response = self.save_company(host="", from_email="", enabled="on")
        self.assertContains(response, "لتفعيل الإرسال أدخل")
        self.assertFalse(EmailSettings.objects.filter(company=self.company, enabled=True).exists())

    def test_notifications_are_emailed_with_link(self):
        from core.notify import notify_managers

        self.save_company(enabled="on")
        with tenant_context(self.company):
            notify_managers(self.company, "طلب إجازة جديد", "/app/leave-application/1/")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["boss@company.test"])
        self.assertIn("hr@company.test", message.from_email)
        self.assertIn("https://erp.company.test/app/leave-application/1/", message.body)

    def test_platform_fallback_and_company_override(self):
        from core.mail import active_config

        platform = EmailSettings.platform()
        platform.enabled, platform.host, platform.from_email = True, "smtp.platform.test", "no-reply@platform.test"
        platform.save()
        self.assertEqual(active_config(self.company).source, "platform")
        self.save_company(enabled="on")
        self.assertEqual(active_config(self.company).source, "company")

    def test_send_test_button(self):
        self.save_company()
        self.client.post(reverse("company_email_settings"), {"action": "test", "test_to": "me@company.test"})
        obj = EmailSettings.objects.get(company=self.company)
        self.assertTrue(obj.last_test_ok)
        self.assertEqual(mail.outbox[-1].to, ["me@company.test"])
        with mock.patch("core.mail.EmailMessage.send", side_effect=OSError("Connection refused")):
            self.client.post(reverse("company_email_settings"), {"action": "test", "test_to": "me@company.test"})
        obj.refresh_from_db()
        self.assertFalse(obj.last_test_ok)
        self.assertIn("Connection refused", obj.last_test_message)

    def test_access_rules(self):
        clerk = User.objects.create_user("clerk", password="x")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER)
        self.client.force_login(clerk)
        self.assertRedirects(self.client.get(reverse("company_email_settings")), reverse("dashboard"),
                             fetch_redirect_response=False)
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(reverse("platform_email_settings")), reverse("dashboard"),
                             fetch_redirect_response=False)
        admin = User.objects.create_superuser("root", "root@x.test", "x")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("platform_email_settings")), "خادم البريد للمنصة")

    def test_profile_email(self):
        self.client.post(reverse("profile"), {"first_name": "مدير", "last_name": "البريد", "email": "new@company.test"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@company.test")

    # ── استعادة كلمة المرور ──
    def test_password_reset_unavailable_without_mail(self):
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("login")), "نسيت كلمة المرور")
        self.assertContains(self.client.get(reverse("password_reset")), "غير مفعّلة")

    def test_password_reset_flow(self):
        self.save_company(enabled="on")
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), "نسيت كلمة المرور")
        response = self.client.post(reverse("password_reset"), {"email": "boss@company.test"})
        self.assertRedirects(response, reverse("password_reset_done"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"https://erp\.company\.test(/password-reset/\S+/)", mail.outbox[0].body).group(1)
        page = self.client.get(link, follow=True)
        self.assertContains(page, "كلمة مرور جديدة")
        response = self.client.post(page.redirect_chain[-1][0], {"new_password1": "Brand#New123",
                                                                  "new_password2": "Brand#New123"})
        self.assertRedirects(response, reverse("password_reset_complete"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Brand#New123"))
        # بريد غير مسجل: نفس الرد دون إرسال
        self.client.post(reverse("password_reset"), {"email": "nobody@company.test"})
        self.assertEqual(len(mail.outbox), 1)
