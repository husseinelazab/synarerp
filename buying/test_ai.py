import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
from django.test import TestCase
from django.urls import reverse

from buying.models import AIInvoiceSettings, PurchaseInvoice, Supplier
from core.models import DocStatus
from core.tenancy import tenant_context
from saas.tests import make_company
from stock.models import UOM, Item, ItemGroup, ItemUOM

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def extracted(**overrides):
    data = {
        "supplier_name": "مؤسسة التوريدات الحديثة", "supplier_tax_id": "300 123 456 700 003",
        "bill_no": "INV-7781", "invoice_date": "2026-10-05", "currency": "SAR",
        "prices_include_vat": False, "tax_rate": 15,
        "lines": [
            {"description": "سكر أبيض 1 كجم", "item_code": "", "barcode": "6281000000017", "unit": "حبة",
             "qty": 10, "unit_price": 5.5, "line_total": 55},
            {"description": "Rice 5kg", "item_code": "RICE5", "barcode": "", "unit": "كيس",
             "qty": 2, "unit_price": 40, "line_total": 80},
        ],
        "invoice_discount": 0, "subtotal": 135, "tax_amount": 20.25, "grand_total": 155.25, "notes": "",
    }
    data.update(overrides)
    return data


class FakeClient:
    """بديل عميل Anthropic: يسجل الطلب ويعيد رداً جاهزاً، فلا اتصال ولا تكلفة في الاختبارات."""

    def __init__(self, result=None, stop_reason="end_turn", error=None):
        self.calls, self.result, self.stop_reason, self.error = [], result or extracted(), stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(retrieve=self._retrieve)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = SimpleNamespace(type="text", text=json.dumps(self.result, ensure_ascii=False))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])

    def _retrieve(self, model):
        self.calls.append({"retrieve": model})
        if self.error:
            raise self.error
        return SimpleNamespace(id=model)


def auth_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=request), body=None)


class AIInvoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الذكاء", "aiowner", plan_code="pro")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.owner)
        group, self.uom = ItemGroup.objects.first(), UOM.objects.first()
        self.sugar = Item.objects.create(code="SUG1", name="سكر", item_group=group, uom=self.uom, barcode="6281000000017")
        self.rice = Item.objects.create(code="RICE5", name="أرز 5 كجم", item_group=group, uom=self.uom)
        self.supplier = Supplier.objects.create(name="التوريدات الحديثة", tax_id="300123456700003")

    def activate(self, model="claude-opus-5-5"):
        cfg = AIInvoiceSettings.for_company(self.company)
        cfg.set_api_key("sk-ant-test-key")
        cfg.enabled, cfg.consent, cfg.model = True, True, model
        cfg.save()
        return cfg

    def upload(self, fake, data=PNG, name="invoice.png"):
        with mock.patch("buying.ai._client", return_value=fake) as client:
            from django.core.files.uploadedfile import SimpleUploadedFile

            response = self.client.post(reverse("ai_invoice_upload"), {"file": SimpleUploadedFile(name, data)})
        return response, client

    def review_post(self, **fields):
        data = {"action": "create", "supplier": self.supplier.pk, "bill_no": "INV-7781", "posting_date": "2026-10-05",
                "include_0": "1", "item_0": self.sugar.pk, "qty_0": "10", "rate_0": "5.50",
                "include_1": "1", "item_1": self.rice.pk, "qty_1": "2", "rate_1": "40"}
        data.update(fields)
        return self.client.post(reverse("ai_invoice_review"), {k: v for k, v in data.items() if v is not None})

    # ---------- الباقة والإعدادات ----------
    def test_basic_plan_has_no_access(self):
        company, owner = make_company("شركة أساسية", "basicowner", plan_code="basic")
        self.client.force_login(owner)
        self.client.get(reverse("switch_company", args=[company.pk]))
        self.assertRedirects(self.client.get(reverse("ai_invoice_upload")), reverse("billing"),
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("ai_settings")), reverse("billing"), fetch_redirect_response=False)
        self.assertNotContains(self.client.get(reverse("dashboard")), reverse("ai_invoice_upload"))

    def test_pro_plan_shows_feature_in_menu(self):
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, reverse("ai_invoice_upload"))
        self.assertContains(page, reverse("ai_settings"))

    def test_settings_require_key_and_consent_and_store_key_encrypted(self):
        url = reverse("ai_settings")
        page = self.client.post(url, {"model": "claude-opus-5-5", "enabled": "on"})
        self.assertContains(page, "أدخل مفتاح API")
        page = self.client.post(url, {"model": "claude-opus-5-5", "enabled": "on", "api_key": "sk-ant-secret-123"})
        self.assertContains(page, "الموافقة")
        self.assertContains(self.client.post(url, {"model": "claude-opus-5-5", "api_key": "wrong-key"}), "sk-ant-")
        self.client.post(url, {"model": "claude-sonnet-5-5", "enabled": "on", "consent": "on",
                               "api_key": "sk-ant-secret-123"})
        cfg = AIInvoiceSettings.objects.get()
        self.assertTrue(cfg.active)
        self.assertEqual(cfg.api_key, "sk-ant-secret-123")
        self.assertNotIn("sk-ant-secret-123", cfg.api_key_encrypted)
        self.assertNotContains(self.client.get(url), "sk-ant-secret-123")
        # الحفظ دون مفتاح يبقي المحفوظ، وحذفه يوقف الخاصية
        self.client.post(url, {"model": "claude-opus-5-5", "enabled": "on", "consent": "on"})
        self.assertEqual(AIInvoiceSettings.objects.get().api_key, "sk-ant-secret-123")
        self.client.post(url, {"model": "claude-opus-5-5", "consent": "on", "clear_key": "on"})
        self.assertFalse(AIInvoiceSettings.objects.get().active)

    def test_regular_user_cannot_open_settings(self):
        from django.contrib.auth.models import User

        from core.models import Membership, Role

        clerk = User.objects.create_user("aiclerk", password="Clerk#Pass123")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER)
        self.client.force_login(clerk)
        self.assertRedirects(self.client.get(reverse("ai_settings")), reverse("dashboard"),
                             fetch_redirect_response=False)

    def test_key_check_button(self):
        self.activate()
        fake = FakeClient()
        with mock.patch("buying.ai._client", return_value=fake):
            page = self.client.post(reverse("ai_settings"), {"action": "test"}, follow=True)
        self.assertContains(page, "المفتاح يعمل")
        self.assertEqual(fake.calls, [{"retrieve": "claude-opus-5-5"}])
        with mock.patch("buying.ai._client", return_value=FakeClient(error=auth_error())):
            page = self.client.post(reverse("ai_settings"), {"action": "test"}, follow=True)
        self.assertContains(page, "مفتاح API غير صحيح")

    # ---------- القراءة والمراجعة ----------
    def test_inactive_feature_makes_no_api_call(self):
        fake = FakeClient()
        response, client = self.upload(fake)
        client.assert_not_called()
        self.assertContains(response, "غير مفعّلة")

    def test_full_flow_creates_matched_draft(self):
        self.activate()
        fake = FakeClient()
        response, _ = self.upload(fake)
        self.assertRedirects(response, reverse("ai_invoice_review"), fetch_redirect_response=False)
        request = fake.calls[0]
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(request["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(request["messages"][0]["content"][0]["type"], "image")
        self.assertEqual(request["messages"][0]["content"][0]["source"]["media_type"], "image/png")

        review = self.client.get(reverse("ai_invoice_review"))
        # الحقول بأرقام قابلة للإرسال، لا بتنسيق اللغة العربية (5,50) الذي لا يُقرأ عند الحفظ
        self.assertContains(review, 'name="rate_0" style="min-width:90px" class="form-control num" dir="ltr" inputmode="decimal" value="5.50"')
        self.assertEqual(review.context["review"]["supplier"], self.supplier)  # بالرقم الضريبي رغم المسافات
        rows = review.context["review"]["rows"]
        self.assertEqual((rows[0]["item_id"], rows[0]["matched_by"]), (self.sugar.pk, "الباركود"))
        self.assertEqual((rows[1]["item_id"], rows[1]["matched_by"]), (self.rice.pk, "كود الصنف"))

        response = self.review_post()
        invoice = PurchaseInvoice.objects.get()
        self.assertRedirects(response, reverse("doc_detail", args=["purchase-invoice", invoice.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(invoice.docstatus, DocStatus.DRAFT)
        self.assertEqual((invoice.supplier, invoice.bill_no, str(invoice.posting_date)),
                         (self.supplier, "INV-7781", "2026-10-05"))
        self.assertEqual(invoice.net_total, Decimal("135.00"))
        self.assertEqual(invoice.grand_total, Decimal("155.25"))
        self.assertEqual(invoice.created_by, self.owner)
        self.assertEqual(AIInvoiceSettings.objects.get().invoices_read, 1)
        self.assertNotIn("ai_invoice", self.client.session)

    def test_vat_inclusive_prices_and_unit_barcode(self):
        self.activate()
        carton, _ = UOM.objects.get_or_create(name="كرتون")
        ItemUOM.objects.create(item=self.sugar, uom=carton, factor=12, barcode="6281000000999")
        lines = [{"description": "سكر كرتون", "item_code": "", "barcode": "6281000000999", "unit": "كرتون",
                  "qty": 1, "unit_price": 115, "line_total": 115}]
        self.upload(FakeClient(extracted(prices_include_vat=True, lines=lines, subtotal=100)))
        row = self.client.get(reverse("ai_invoice_review")).context["review"]["rows"][0]
        self.assertEqual((row["item_id"], row["uom_id"], row["rate"]), (self.sugar.pk, carton.pk, Decimal("100.00")))
        self.review_post(include_1=None, rate_0="100", qty_0="1")
        line = PurchaseInvoice.objects.get().items.get()
        self.assertEqual((line.uom_id, line.stock_qty), (carton.pk, Decimal("12.000")))

    def test_unmatched_item_must_be_chosen(self):
        self.activate()
        lines = extracted()["lines"] + [{"description": "صنف غير معروف", "item_code": "", "barcode": "", "unit": "",
                                         "qty": 1, "unit_price": 3, "line_total": 3}]
        self.upload(FakeClient(extracted(lines=lines)))
        review = self.client.get(reverse("ai_invoice_review"))
        self.assertIsNone(review.context["review"]["rows"][2]["item_id"])
        page = self.review_post(include_2="1", item_2="", qty_2="1", rate_2="3")
        self.assertContains(page, "اختر الصنف المقابل")
        self.assertFalse(PurchaseInvoice.objects.exists())
        self.review_post()  # إلغاء تحديد البند غير المطابق
        self.assertEqual(PurchaseInvoice.objects.get().items.count(), 2)

    def test_new_supplier_created_from_invoice(self):
        self.activate()
        self.upload(FakeClient(extracted(supplier_name="مورد جديد تماماً", supplier_tax_id="311111111100003")))
        self.assertIsNone(self.client.get(reverse("ai_invoice_review")).context["review"]["supplier"])
        self.review_post(supplier="new", new_supplier_name="مورد جديد تماماً", new_supplier_tax_id="311111111100003")
        self.assertEqual(PurchaseInvoice.objects.get().supplier.tax_id, "311111111100003")
        self.upload(FakeClient())  # إنشاء المسودة يمسح القراءة السابقة
        page = self.review_post(supplier="new", new_supplier_name="التوريدات الحديثة")
        self.assertContains(page, "يوجد مورد باسم")

    def test_matching_never_uses_another_companys_data(self):
        other, _ = make_company("شركة أخرى", "otherowner")
        with tenant_context(other):
            Supplier.objects.create(name="مورد الشركة الأخرى", tax_id="399999999900003")
        self.activate()
        self.upload(FakeClient(extracted(supplier_name="مورد الشركة الأخرى", supplier_tax_id="399999999900003")))
        self.assertIsNone(self.client.get(reverse("ai_invoice_review")).context["review"]["supplier"])
        with tenant_context(other):
            foreign = Supplier.objects.get()
        page = self.review_post(supplier=foreign.pk)
        self.assertContains(page, "اختر المورد")
        self.assertFalse(PurchaseInvoice.objects.exists())

    def test_rejects_non_invoice_files_without_api_call(self):
        self.activate()
        fake = FakeClient()
        response, client = self.upload(fake, data=b"MZ\x90\x00 not an image", name="invoice.png")
        client.assert_not_called()  # فحص نوع الملف يسبق أي اتصال بـ Anthropic
        self.assertRedirects(response, reverse("ai_invoice_upload"), fetch_redirect_response=False)

    def test_api_errors_and_refusals_are_explained(self):
        self.activate()
        page = self.upload(FakeClient(error=auth_error()))[0]
        self.assertIn("مفتاح API غير صحيح", [str(m) for m in page.wsgi_request._messages][0])
        self.upload(FakeClient(stop_reason="refusal"))
        page = self.client.get(reverse("ai_invoice_upload"))
        self.assertContains(page, "اعتذر النموذج")
        self.assertFalse(PurchaseInvoice.objects.exists())

    def test_haiku_is_sent_without_server_fallback(self):
        self.activate(model="claude-haiku-5-5")
        fake = FakeClient()
        self.upload(fake)
        self.assertNotIn("fallbacks", fake.calls[0])
        self.assertNotIn("betas", fake.calls[0])

    def test_pdf_is_sent_as_document(self):
        self.activate()
        fake = FakeClient()
        self.upload(fake, data=b"%PDF-1.7\n" + b"0" * 32, name="invoice.pdf")
        self.assertEqual(fake.calls[0]["messages"][0]["content"][0]["type"], "document")


class NumberParsingTests(TestCase):
    def test_user_typed_numbers(self):
        from buying.ai import to_decimal

        self.assertEqual(to_decimal("٥٫٥٠"), Decimal("5.50"))
        self.assertEqual(to_decimal("5,50"), Decimal("5.50"))
        self.assertEqual(to_decimal("1,250.75"), Decimal("1250.75"))
        self.assertEqual(to_decimal("١٠", 3), Decimal("10.000"))
        self.assertEqual(to_decimal(12.5), Decimal("12.50"))
        self.assertEqual(to_decimal("abc"), Decimal("0.00"))
