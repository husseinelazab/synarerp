import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core.models import ApiKey, Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice, SalesOrder
from stock.models import UOM, Item, ItemGroup, Warehouse


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الربط", "apiowner")
        cls.other, cls.other_owner = make_company("شركة أخرى", "otherowner")

    def setUp(self):
        cache.clear()
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.key, self.raw = ApiKey.issue(self.company, self.owner, "اختبار")
        self.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        self.service = Item.objects.create(code="SRV", name="خدمة", item_group=ItemGroup.objects.first(),
                                           uom=UOM.objects.first(), is_stock_item=False, selling_rate=150)

    def call(self, method, path, data=None, key=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {key or self.raw}"}
        body = json.dumps(data) if data is not None else None
        return getattr(self.client, method)(path, data=body, content_type="application/json", **headers)

    def test_authentication(self):
        self.assertEqual(self.client.get("/api/v1/").status_code, 401)
        self.assertEqual(self.call("get", "/api/v1/", key="snr_bad_key").status_code, 401)
        info = self.call("get", "/api/v1/").json()
        self.assertEqual(info["company"], "شركة الربط")
        self.assertIn("sales-invoice", [d["slug"] for d in info["doctypes"]])
        self.key.revoked = True
        self.key.save()
        self.assertEqual(self.call("get", "/api/v1/").status_code, 401)
        self.assertNotIn(self.raw, ApiKey.objects.get().key_hash)

    def test_create_customer_and_invoice_with_submit(self):
        response = self.call("post", "/api/v1/customer/", {"name": "عميل من المتجر", "phone": "0500000000"})
        self.assertEqual(response.status_code, 201, response.content)
        customer_id = response.json()["id"]
        response = self.call("post", "/api/v1/sales-invoice/", {
            "customer": customer_id, "update_stock": False, "tax_rate": "15", "vat_category": "S",
            "items": [{"item": self.service.pk, "qty": "2", "rate": "150"}], "submit": True})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual((data["docstatus"], data["grand_total"], data["customer_display"]),
                         (1, "345.00", "عميل من المتجر"))
        self.assertEqual(len(data["items"]), 1)
        invoice = SalesInvoice.objects.get(pk=data["id"])
        self.assertEqual(invoice.created_by, self.owner)
        listing = self.call("get", f"/api/v1/sales-invoice/?customer={customer_id}&docstatus=1").json()
        self.assertEqual((listing["count"], listing["results"][0]["name"]), (1, invoice.name))
        detail = self.call("get", f"/api/v1/sales-invoice/{invoice.pk}/").json()
        self.assertEqual(detail["tax_amount"], "45.00")
        # المعتمد لا يُعدّل ولا يُحذف
        self.assertEqual(self.call("patch", f"/api/v1/sales-invoice/{invoice.pk}/", {"remarks": "x"}).status_code, 409)
        self.assertEqual(self.call("delete", f"/api/v1/sales-invoice/{invoice.pk}/").status_code, 409)
        cancelled = self.call("post", f"/api/v1/sales-invoice/{invoice.pk}/cancel/").json()
        self.assertEqual(cancelled["document"]["docstatus"], 2)

    def test_validation_errors_are_reported_per_field(self):
        response = self.call("post", "/api/v1/sales-invoice/", {"items": [{"item": 999999, "qty": "1"}]})
        self.assertEqual(response.status_code, 400)
        fields = response.json()["error"]["fields"]
        self.assertIn("customer", fields)
        self.assertIn("items[0].item", fields)
        bad = self.client.post("/api/v1/customer/", data="{not json", content_type="application/json",
                               HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(bad.status_code, 400)

    def test_submit_failure_keeps_draft_and_reports(self):
        stock_item = Item.objects.create(code="STK", name="بضاعة", item_group=ItemGroup.objects.first(),
                                         uom=UOM.objects.first(), selling_rate=10)
        customer = Customer.objects.create(name="عميل")
        response = self.call("post", "/api/v1/sales-invoice/", {
            "customer": customer.pk, "update_stock": True, "warehouse": self.wh.pk, "tax_rate": "15",
            "vat_category": "S", "items": [{"item": stock_item.pk, "qty": "5", "rate": "10"}], "submit": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("الرصيد غير كافٍ", response.json()["error"]["message"])
        self.assertEqual(SalesInvoice.objects.get().docstatus, 0)

    def test_patch_draft_replaces_items_and_actions(self):
        customer = Customer.objects.create(name="عميل الأوامر")
        created = self.call("post", "/api/v1/sales-order/", {
            "customer": customer.pk, "tax_rate": "15", "items": [{"item": self.service.pk, "qty": "1", "rate": "100"}]})
        order_id = created.json()["id"]
        patched = self.call("patch", f"/api/v1/sales-order/{order_id}/", {
            "items": [{"item": self.service.pk, "qty": "3", "rate": "100"}]}).json()
        self.assertEqual((patched["total"], len(patched["items"])), ("300.00", 1))
        self.call("post", f"/api/v1/sales-order/{order_id}/submit/")
        result = self.call("post", f"/api/v1/sales-order/{order_id}/make_invoice/").json()
        self.assertEqual(result["created"]["doctype"], "sales-invoice")
        self.assertTrue(SalesInvoice.objects.filter(pk=result["created"]["id"], order_id=order_id).exists())
        self.assertEqual(self.call("post", f"/api/v1/sales-order/{order_id}/hack_me/").status_code, 400)

    def test_user_permissions_apply(self):
        clerk = User.objects.create_user("apiclerk", password="x")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER, modules=["stock"],
                                  can_submit=False)
        _, raw = ApiKey.issue(self.company, clerk, "مخزون فقط")
        self.assertEqual(self.call("get", "/api/v1/sales-invoice/", key=raw).status_code, 403)
        self.assertEqual(self.call("get", "/api/v1/item/", key=raw).status_code, 200)
        item = self.call("post", "/api/v1/item/", {"code": "N1", "name": "جديد", "item_group": ItemGroup.objects.first().pk,
                                                   "uom": UOM.objects.first().pk}, key=raw)
        self.assertEqual(item.status_code, 201, item.content)
        self.assertEqual(self.call("get", "/api/v1/reports/account-balances/", key=raw).status_code, 403)
        self.assertEqual(self.call("get", "/api/v1/reports/stock-balance/", key=raw).status_code, 200)

    def test_tenant_isolation(self):
        with tenant_context(self.other):
            foreign = Customer.objects.create(name="عميل شركة أخرى")
        self.assertEqual(self.call("get", f"/api/v1/customer/{foreign.pk}/").status_code, 404)
        names = [c["name"] for c in self.call("get", "/api/v1/customer/").json()["results"]]
        self.assertNotIn("عميل شركة أخرى", names)

    def test_rate_limit(self):
        from core import api
        original = api.RATE_LIMIT_PER_MINUTE
        api.RATE_LIMIT_PER_MINUTE = 3
        try:
            codes = [self.call("get", "/api/v1/").status_code for _ in range(4)]
        finally:
            api.RATE_LIMIT_PER_MINUTE = original
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_key_management_pages(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("api_keys"), {"name": "المتجر", "member": Membership.objects.get(
            user=self.owner, company=self.company).pk})
        new_key = response.context["new_key"]
        self.assertTrue(new_key.startswith("snr_"))
        self.assertEqual(self.call("get", "/api/v1/", key=new_key).status_code, 200)
        self.assertContains(self.client.get(reverse("api_docs")), "sales-invoice")
