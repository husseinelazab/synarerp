import copy
import hashlib
import hmac
import json
from decimal import Decimal

from django.test import TestCase

from accounts.models import Account
from buying.models import PurchaseInvoice, Supplier
from core.tenancy import tenant_context
from ecommerce.models import ExternalOrder, SkuMapping, StoreConnection
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse

SALLA_ORDER = {
    "event": "order.created", "merchant": 1234509, "created_at": "2026-10-11 10:00:00",
    "data": {
        "id": 98765, "reference_id": 22015, "date": {"date": "2026-10-11 09:58:00.000000", "timezone": "Asia/Riyadh"},
        "status": {"slug": "under_review", "name": "بإنتظار المراجعة"},
        "payment_method": "credit_card", "currency": "SAR",
        "amounts": {"sub_total": {"amount": 230, "currency": "SAR"}, "shipping_cost": {"amount": 23, "currency": "SAR"},
                    "discounts": [], "tax": {"amount": {"amount": 33, "currency": "SAR"}},
                    "total": {"amount": 253, "currency": "SAR"}},
        "customer": {"id": 77, "first_name": "نورة", "last_name": "القحطاني", "mobile": "555123456", "mobile_code": "+966",
                     "email": "noura@example.com", "city": "الرياض"},
        "items": [{"name": "عطر عود", "sku": "OUD-50", "quantity": 2,
                   "amounts": {"price_without_tax": {"amount": 100, "currency": "SAR"},
                               "total": {"amount": 230, "currency": "SAR"}}}],
    },
}

ZID_ORDER = {
    "id": 5551, "code": "ZD-5551", "store_id": "777", "created_at": "2026-10-11 11:00:00",
    "order_status": {"code": "new"}, "currency_code": "SAR", "payment": {"method": {"code": "zid_cod"}},
    "customer": {"name": "خالد", "mobile": "0501112233", "email": ""},
    "products": [{"sku": "OUD-50", "name": "عطر عود", "quantity": 1, "price": "115.00", "total": "115.00"}],
    "shipping": {"method": {"shipping_fee": "0"}}, "order_total": "115.00",
}


class EcommerceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("متجر العطور", "perfumes")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.wh = Warehouse.objects.first()
        self.oud = Item.objects.create(code="OUD-50", name="عطر عود 50مل", item_group=ItemGroup.objects.first(),
                                       uom=UOM.objects.first(), selling_rate=115)
        stock = PurchaseInvoice.objects.create(supplier=Supplier.objects.create(name="مصنع"), warehouse=self.wh)
        stock.items.create(item=self.oud, qty=10, rate=40)
        stock.submit()
        self.gateway = Account.objects.get(number="1102")
        self.salla = StoreConnection.objects.create(platform="salla", name="متجر سلة", warehouse=self.wh,
                                                    webhook_secret="s3cret", payment_account=self.gateway,
                                                    store_id="1234509")
        self.zid = StoreConnection.objects.create(platform="zid", name="متجر زد", warehouse=self.wh,
                                                  payment_account=self.gateway)

    def post(self, connection, body, signed=True, query=""):
        raw = json.dumps(body).encode()
        headers = {}
        if signed and connection.webhook_secret:
            headers["HTTP_X_SALLA_SIGNATURE"] = hmac.new(connection.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(connection.webhook_path() + query, raw, content_type="application/json", **headers)

    def test_salla_paid_order_becomes_invoice_and_receipt(self):
        response = self.post(self.salla, SALLA_ORDER)
        self.assertEqual(response.json()["status"], "imported")
        order = ExternalOrder.objects.get()
        invoice = order.invoice
        self.assertTrue(invoice.is_submitted)
        self.assertEqual(invoice.grand_total, Decimal("253.00"))
        self.assertEqual(invoice.customer.phone, "966555123456")
        self.assertEqual(invoice.items.count(), 2)                          # العطر + الشحن
        self.assertEqual(Bin.objects.get(item=self.oud, warehouse=self.wh).actual_qty, 8)
        invoice.refresh_from_db()
        self.assertEqual(invoice.outstanding_amount, 0)                     # دفع إلكتروني ← سند قبض
        self.assertEqual(order.payment.account, self.gateway)
        # إعادة الإرسال من المنصة لا تكرر الفاتورة، والعميل نفسه يُعاد استخدامه
        self.post(self.salla, SALLA_ORDER)
        self.assertEqual(SalesInvoice.objects.count(), 1)
        second = copy.deepcopy(SALLA_ORDER)
        second["data"]["id"] = 98766
        self.post(self.salla, second)
        self.assertEqual(Customer.objects.filter(email="noura@example.com").count(), 1)

    def test_signature_and_store_checks(self):
        self.assertEqual(self.post(self.salla, SALLA_ORDER, signed=False).status_code, 403)
        raw = json.dumps(SALLA_ORDER).encode()
        bad = self.client.post(self.salla.webhook_path(), raw, content_type="application/json",
                               HTTP_X_SALLA_SIGNATURE="00" * 32)
        self.assertEqual(bad.status_code, 403)
        token = self.client.post(self.salla.webhook_path(), raw, content_type="application/json",
                                 HTTP_AUTHORIZATION="s3cret")
        self.assertEqual(token.status_code, 200)                            # استراتيجية الرمز في سلة
        other = copy.deepcopy(SALLA_ORDER)
        other["merchant"] = 1
        other["data"]["id"] = 1
        self.assertEqual(self.post(self.salla, other).status_code, 403)
        self.assertEqual(self.client.post("/integrations/salla/wrong-key/", raw, content_type="application/json").status_code, 404)
        app_event = {"event": "app.store.authorize", "merchant": 1234509, "data": {"access_token": "x"}}
        self.assertEqual(self.post(self.salla, app_event).json()["status"], "authorized")
        stranger = {"event": "app.store.authorize", "merchant": 1, "data": {"access_token": "evil"}}
        self.assertEqual(self.post(self.salla, stranger).json()["status"], "ignored")   # متجر آخر
        self.salla.refresh_from_db()
        self.assertEqual(self.salla.access_token, "x")

    def test_unknown_sku_review_then_map(self):
        body = copy.deepcopy(SALLA_ORDER)
        body["data"]["items"][0]["sku"] = "SALLA-OUD"
        self.post(self.salla, body)
        order = ExternalOrder.objects.get()
        self.assertEqual(order.status, "review")
        self.assertIn("SALLA-OUD", order.error)
        self.client.force_login(self.user)
        page = self.client.get(order.get_absolute_url())
        self.assertContains(page, "ربط الأصناف غير المعرّفة")
        self.client.post(order.get_absolute_url(), {"action": "map", "sku": "SALLA-OUD", "item": self.oud.pk})
        order.refresh_from_db()
        self.assertEqual(order.status, "imported")
        self.assertTrue(SkuMapping.objects.filter(sku="SALLA-OUD", item=self.oud).exists())

    def test_total_mismatch_and_stock_shortage_go_to_review(self):
        body = copy.deepcopy(SALLA_ORDER)
        body["data"]["amounts"]["total"]["amount"] = 300
        self.post(self.salla, body)
        order = ExternalOrder.objects.get()
        self.assertEqual(order.status, "review")
        self.assertTrue(order.invoice.is_draft)
        big = copy.deepcopy(SALLA_ORDER)
        big["data"]["id"] = 2
        big["data"]["items"][0]["quantity"] = 50
        big["data"]["items"][0]["amounts"]["total"]["amount"] = 5750
        big["data"]["amounts"]["total"]["amount"] = 5773
        self.post(self.salla, big)
        short = ExternalOrder.objects.get(external_id="2")
        self.assertEqual(short.status, "review")
        self.assertIn("تعذر اعتماد", short.error)

    def test_zid_cod_order_and_cancel(self):
        response = self.post(self.zid, ZID_ORDER, query="?event=order.create")
        self.assertEqual(response.json()["status"], "imported")
        order = ExternalOrder.objects.get(connection=self.zid)
        self.assertIsNone(order.payment)                                    # الدفع عند الاستلام ← ذمة على العميل
        self.assertEqual(order.invoice.outstanding_amount, Decimal("115.00"))
        cancelled = copy.deepcopy(ZID_ORDER)
        cancelled["order_status"] = {"code": "cancelled"}
        with self.captureOnCommitCallbacks(execute=True):
            self.post(self.zid, cancelled, query="?event=order.status.update")
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertTrue(SalesInvoice.objects.filter(is_return=True, return_against=order.invoice, docstatus=0).exists())

    def test_cancel_before_submit_deletes_draft_and_test_page(self):
        self.salla.auto_submit = False
        self.salla.save()
        self.post(self.salla, SALLA_ORDER)
        order = ExternalOrder.objects.get()
        self.assertTrue(order.invoice.is_draft)
        cancelled = copy.deepcopy(SALLA_ORDER)
        cancelled["event"] = "order.status.updated"
        cancelled["data"]["status"] = {"slug": "canceled"}
        self.post(self.salla, cancelled)
        order.refresh_from_db()
        self.assertEqual((order.status, order.invoice), ("cancelled", None))
        self.assertEqual(SalesInvoice.objects.count(), 0)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.zid.get_absolute_url()), "/integrations/zid/")
        body = copy.deepcopy(ZID_ORDER)
        response = self.client.post(f"/ecommerce/stores/{self.zid.pk}/test/", {"payload": json.dumps(body)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/ecommerce/orders/").status_code, 200)
