import io
import json
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from buying.models import PurchaseInvoice, Supplier
from core.tenancy import tenant_context
from ecommerce import api
from ecommerce.models import StoreConnection, StoreProduct
from ecommerce.sync import import_products, sync_stock
from saas.tests import make_company
from stock.models import UOM, Item, ItemGroup, Warehouse


class FakeAPI:
    """يستبدل الاتصال بالمنصة: يسجل الطلبات ويعيد ردوداً حسب العنوان."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data) if request.data and request.headers.get("Content-type") == "application/json" else request.data
        self.calls.append((request.get_method(), request.full_url, dict(request.header_items()), body))
        for fragment, payload in self.routes.items():
            if fragment in request.full_url:
                return io.BytesIO(json.dumps(payload).encode())
        return io.BytesIO(b"{}")


SALLA_PRODUCTS = {"data": [
    {"id": 1, "name": "عطر عود", "sku": "OUD-50", "price": {"amount": 115, "currency": "SAR"}, "quantity": 3},
    {"id": 2, "name": "بخور", "sku": "BKH-1", "price": {"amount": 40}, "quantity": None, "unlimited_quantity": True},
    {"id": 3, "name": "بلا رمز", "sku": "", "price": 10, "quantity": 1},
], "pagination": {"currentPage": 1, "totalPages": 1}}


class StoreSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("متجر المزامنة", "syncer")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.wh = Warehouse.objects.first()
        self.oud = Item.objects.create(code="OUD-50", name="عطر عود", item_group=ItemGroup.objects.first(),
                                       uom=UOM.objects.first())
        stock = PurchaseInvoice.objects.create(supplier=Supplier.objects.create(name="مصنع"), warehouse=self.wh)
        stock.items.create(item=self.oud, qty=12, rate=40)
        stock.submit()
        self.salla = StoreConnection.objects.create(platform="salla", name="سلة", warehouse=self.wh, stock_buffer=2,
                                                    sync_stock=True)
        self.salla.set_tokens("tok-123")
        self.salla.save()

    def fake(self, routes):
        fake = FakeAPI(routes)
        patcher = mock.patch.object(api, "_urlopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_tokens_are_encrypted(self):
        self.salla.refresh_from_db()
        self.assertNotIn("tok-123", self.salla.access_token_enc)
        self.assertEqual(self.salla.access_token, "tok-123")

    def test_import_products_links_and_creates(self):
        fake = self.fake({"/products": SALLA_PRODUCTS})
        stats = import_products(self.salla)
        self.assertEqual(stats, {"total": 3, "linked": 1, "created": 0, "unlinked": 2})
        self.assertEqual(fake.calls[0][2]["Authorization"], "Bearer tok-123")
        stats = import_products(self.salla, create_missing=True)
        self.assertEqual(stats["created"], 1)                                # البخور؛ المنتج بلا SKU لا يُنشأ
        self.assertEqual(Item.objects.get(code="BKH-1").selling_rate, Decimal("40"))
        self.assertIsNone(StoreProduct.objects.get(sku="BKH-1").store_quantity)  # كمية غير محدودة

    def test_sync_sends_only_changes_with_buffer(self):
        fake = self.fake({"/products/quantities/bulk": {"success": True}, "/products": SALLA_PRODUCTS})
        import_products(self.salla)
        self.assertEqual(sync_stock(self.salla), 1)
        method, url, headers, body = fake.calls[-1]
        self.assertEqual((method, url), ("POST", "https://api.salla.dev/admin/v2/products/quantities/bulk"))
        self.assertEqual(body["products"], [{"identifer_type": "sku", "identifer": "OUD-50", "quantity": 10,
                                             "mode": "overwrite"}])       # 12 − احتياطي 2
        self.assertEqual(sync_stock(self.salla), 0)                          # لا تغيير ← لا طلب
        call_command("sync_store_stock", stdout=io.StringIO(), stderr=io.StringIO())
        self.salla.refresh_from_db()
        self.assertEqual(self.salla.last_sync_message, "لا تغييرات في الكميات")

    def test_zid_headers_and_bulk_patch(self):
        zid = StoreConnection.objects.create(platform="zid", name="زد", warehouse=self.wh, store_id="777")
        zid.set_tokens("access-1", partner="partner-1")
        zid.save()
        fake = self.fake({"/products/?page": {"count": 1, "next": None, "results": [
            {"id": "a1b2", "sku": "OUD-50", "name": {"ar": "عطر عود"}, "price": "115.00", "quantity": 5}]}})
        import_products(zid)
        headers = fake.calls[0][2]
        self.assertEqual((headers["Authorization"], headers["Access-token"], headers["Store-id"], headers["Role"]),
                         ("Bearer partner-1", "access-1", "777", "Manager"))
        sync_stock(zid)
        method, url, _, body = fake.calls[-1]
        self.assertEqual((method, url), ("PATCH", "https://api.zid.sa/v1/products/"))
        self.assertEqual(body, [{"id": "a1b2", "sku": "OUD-50", "quantity": 12, "is_infinite": False}])

    def test_api_error_is_recorded(self):
        import urllib.error

        def broken(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"token"}'))
        patcher = mock.patch.object(api, "_urlopen", broken)
        patcher.start()
        self.addCleanup(patcher.stop)
        StoreProduct.objects.create(connection=self.salla, external_id="1", sku="OUD-50", item=self.oud)
        with self.assertRaises(api.ApiError):
            sync_stock(self.salla)
        self.salla.refresh_from_db()
        self.assertIn("401", self.salla.last_sync_message)

    def test_salla_authorize_event_and_pages(self):
        connection = StoreConnection.objects.create(platform="salla", name="سلة 2", warehouse=self.wh)
        body = {"event": "app.store.authorize", "merchant": 4455, "created_at": "2026-10-11",
                "data": {"access_token": "new-token", "refresh_token": "ref", "expires": 1893456000, "scope": "x"}}
        response = self.client.post(connection.webhook_path(), json.dumps(body), content_type="application/json")
        self.assertEqual(response.json()["status"], "authorized")
        connection.refresh_from_db()
        self.assertEqual((connection.access_token, connection.store_id), ("new-token", "4455"))
        self.assertEqual(connection.token_expires_at.year, 2030)
        self.client.force_login(self.user)
        self.fake({"/oauth2/user/info": {"data": {"merchant": {"id": 4455, "name": "متجر العطور"}}}})
        response = self.client.post(f"/ecommerce/stores/{connection.pk}/action/", {"action": "test"}, follow=True)
        self.assertContains(response, "متجر العطور")
        StoreProduct.objects.create(connection=connection, external_id="9", sku="X-1", name="منتج")
        page = self.client.get(f"/ecommerce/stores/{connection.pk}/products/")
        self.assertContains(page, "X-1")
        product = StoreProduct.objects.get(sku="X-1")
        self.client.post(f"/ecommerce/stores/{connection.pk}/products/", {"product": product.pk, "item": self.oud.pk})
        product.refresh_from_db()
        self.assertEqual(product.item, self.oud)
