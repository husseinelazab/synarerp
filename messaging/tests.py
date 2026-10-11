import datetime
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Company, Membership, Role
from messaging.models import MessageLog, MessageTemplate, MessagingSettings
from messaging.service import compose, normalize_phone
from selling.models import Customer, SalesInvoice
from stock.models import Item


class FakeResponse:
    def __init__(self, status=200, body=b'{"ok":true}'):
        self.status, self.body = status, body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@override_settings(SYNAR_MESSAGING_ASYNC=False)
class MessagingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.customer = Customer.objects.filter(phone__gt="").first()
        cls.item = Item.objects.get(code="PRN-001")

    def setUp(self):
        self.client.force_login(self.owner)

    def invoice(self, due_days=30, submit=True):
        inv = SalesInvoice.objects.create(company=self.company, customer=self.customer,
                                          due_date=timezone.localdate() + datetime.timedelta(days=due_days),
                                          update_stock=False)
        inv.items.create(item=self.item, qty=1, rate=1000)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
        return inv

    def activate(self, provider="taqnyat", **extra):
        from core.mail import encrypt_password

        cfg = MessagingSettings.for_company(self.company)
        cfg.provider, cfg.sender, cfg.enabled = provider, "SYNAR", True
        cfg.token_encrypted = encrypt_password("secret-token")
        for k, v in extra.items():
            setattr(cfg, k, v)
        cfg.save()
        return cfg

    def test_phone_normalization_and_compose(self):
        self.assertEqual(normalize_phone("0501234567"), "966501234567")
        self.assertEqual(normalize_phone("+966 50 123 4567"), "966501234567")
        self.assertEqual(normalize_phone("00971501234567"), "971501234567")
        self.assertEqual(normalize_phone("501234567"), "966501234567")
        self.assertEqual(normalize_phone("12"), "")
        text = compose(self.company, "statement", customer="أحمد", balance=Decimal("1500"), overdue_text="")
        self.assertIn("أحمد", text)
        self.assertIn("1,500.00", text)
        MessageTemplate.objects.create(company=self.company, event="statement", body="رصيدك {balance} يا {customer}")
        self.assertEqual(compose(self.company, "statement", customer="سعد", balance="10"), "رصيدك 10 يا سعد")

    @mock.patch("messaging.service.urllib.request.urlopen")
    def test_default_is_whatsapp_link_without_network(self, urlopen):
        inv = self.invoice()
        response = self.client.post(f"/messaging/send/invoice/{inv.pk}/", {"mode": "link"})
        self.assertTrue(response["Location"].startswith("https://wa.me/966"))
        self.assertIn(inv.name, response["Location"].replace("%2D", "-"))
        self.assertEqual(MessageLog.objects.get().status, "link")
        # الإرسال عبر المزوّد مرفوض قبل التفعيل
        self.client.post(f"/messaging/send/invoice/{inv.pk}/", {"mode": "provider"})
        urlopen.assert_not_called()
        self.assertEqual(MessageLog.objects.count(), 1)

    @mock.patch("messaging.service.urllib.request.urlopen", return_value=FakeResponse())
    def test_providers_request_shapes(self, urlopen):
        cases = {
            "taqnyat": ("https://api.taqnyat.sa/v1/messages", lambda b: b["recipients"] == ["966500000001"]),
            "whatsapp": ("https://graph.facebook.com/v20.0/SYNAR/messages",
                         lambda b: b["messaging_product"] == "whatsapp" and b["type"] == "text"),
            "msegat": ("https://www.msegat.com/gw/sendsms.php", lambda b: b["numbers"] == "966500000001"),
        }
        for provider, (url, check) in cases.items():
            with self.subTest(provider=provider):
                urlopen.reset_mock()
                urlopen.return_value = FakeResponse(body=b'{"code":"1"}' if provider == "msegat" else b"{}")
                cfg = self.activate(provider, account="user1")
                from messaging.service import send

                entry, link = send(self.company, "0500000001", "مرحبا", mode="provider")
                req = urlopen.call_args[0][0]
                self.assertEqual(req.full_url, url)
                self.assertTrue(check(json.loads(req.data)))
                self.assertEqual((entry.status, link), ("sent", ""))
                if provider != "msegat":
                    self.assertEqual(req.headers["Authorization"], "Bearer secret-token")
        # يونيفونك بنموذج form، والقالب المعتمد لواتساب
        urlopen.return_value = FakeResponse()
        self.activate("unifonic", account="APPSID")
        from messaging.service import send

        send(self.company, "0500000001", "نص", mode="provider")
        self.assertIn(b"AppSid=APPSID", urlopen.call_args[0][0].data)
        self.activate("whatsapp", whatsapp_template="invoice_notice")
        send(self.company, "0500000001", "نص", mode="provider")
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body["template"]["name"], "invoice_notice")
        self.assertEqual(body["template"]["components"][0]["parameters"][0]["text"], "نص")

    @mock.patch("messaging.service.urllib.request.urlopen", return_value=FakeResponse())
    def test_generic_http_provider_escapes_text(self, urlopen):
        self.activate("http", api_url="https://sms.example.com/send",
                      http_body='{"phone": "{to}", "text": "{message}", "from": "{sender}"}')
        from messaging.service import send

        send(self.company, "0500000001", 'قال "مرحبا"\nسطر', mode="provider")
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body, {"phone": "966500000001", "text": 'قال "مرحبا"\nسطر', "from": "SYNAR"})

    def test_failed_send_logged(self):
        import urllib.error

        self.activate()
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        err.read = lambda: b'{"message":"invalid token"}'
        with mock.patch("messaging.service.urllib.request.urlopen", side_effect=err):
            from messaging.service import send

            entry, _ = send(self.company, "0500000001", "x", mode="provider")
        self.assertEqual(entry.status, "failed")
        self.assertIn("invalid token", entry.error)

    @mock.patch("messaging.service.urllib.request.urlopen", return_value=FakeResponse())
    def test_auto_invoice_and_payment_messages(self, urlopen):
        self.activate(auto_invoice=True, auto_payment=True)
        with self.captureOnCommitCallbacks(execute=True):
            inv = self.invoice()
        log = MessageLog.objects.get(event="invoice")
        self.assertEqual((log.reference, log.status), (inv.name, "sent"))
        from accounts.models import PaymentEntry

        with self.captureOnCommitCallbacks(execute=True):
            pe = PaymentEntry.objects.create(company=self.company, payment_type="receive", customer=self.customer,
                                             account=self.company.default_cash_account, amount=500, sales_invoice=inv)
            pe.submit()
        self.assertTrue(MessageLog.objects.filter(event="payment", reference=pe.name).exists())

    @mock.patch("messaging.service.urllib.request.urlopen", return_value=FakeResponse())
    def test_overdue_reminders_once_per_period(self, urlopen):
        inv = self.invoice(due_days=30)
        SalesInvoice.objects.filter(pk=inv.pk).update(due_date=timezone.localdate() - datetime.timedelta(days=10))
        call_command("send_reminders", stdout=open("/dev/null", "w"))
        self.assertFalse(MessageLog.objects.exists())  # التذكير الآلي غير مفعّل
        self.activate(auto_overdue=True, overdue_every_days=7)
        call_command("send_reminders", stdout=open("/dev/null", "w"))
        call_command("send_reminders", stdout=open("/dev/null", "w"))
        logs = MessageLog.objects.filter(event="overdue")
        self.assertEqual(logs.count(), 1)
        self.assertIn("متأخرة 10 يوماً", logs.get().body)

    @mock.patch("messaging.service.urllib.request.urlopen", return_value=FakeResponse())
    def test_settings_page_save_test_and_enable(self, urlopen):
        data = {"provider": "taqnyat", "sender": "SYNAR", "account": "", "token": "", "api_url": "",
                "http_body": "{}", "whatsapp_template": "", "whatsapp_language": "ar", "country_code": "966",
                "overdue_every_days": "7", "enabled": "on", "tpl_invoice": "فاتورة {invoice}"}
        page = self.client.post("/settings/messaging/", data)
        self.assertContains(page, "أكمل بيانات المزوّد")  # التفعيل بلا رمز مرفوض
        data["token"] = "abc"
        self.assertEqual(self.client.post("/settings/messaging/", data).status_code, 302)
        cfg = MessagingSettings.objects.get(company=self.company)
        self.assertTrue(cfg.active)
        self.assertNotIn("abc", cfg.token_encrypted)
        self.assertEqual(cfg.token, "abc")
        self.assertEqual(MessageTemplate.text_for(self.company, "invoice"), "فاتورة {invoice}")
        self.client.post("/settings/messaging/", {"action": "test", "test_to": "0500000001"})
        cfg.refresh_from_db()
        self.assertTrue(cfg.last_test_ok)
        self.assertContains(self.client.get("/settings/messaging/log/"), "رسالة اختبار")
        inv = self.invoice()
        self.assertContains(self.client.get(f"/app/sales-invoice/{inv.pk}/"), "إرسال رسالة")
