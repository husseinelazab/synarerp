from django.test import TestCase

from core.models import Membership, Role
from core.tenancy import tenant_context
from pos.models import POSProfile, POSShift
from pos.services import checkout, pos_return
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import Item, ItemGroup, UOM


class InvoiceSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("متجر البحث", "search_cashier", role=Role.CASHIER)
        cls.other, cls.other_user = make_company("متجر آخر", "search_other")
        with tenant_context(cls.company):
            cls.profile = POSProfile.objects.get()
            cls.customer = Customer.objects.create(name="أحمد مؤسسة النور", phone="0559876543")
            cls.item = Item.objects.create(code="LOOKUP", name="خدمة تجريبية", item_group=ItemGroup.objects.first(),
                uom=UOM.objects.first(), is_stock_item=False, selling_rate=100)
        with tenant_context(cls.other):
            other_profile = POSProfile.objects.get()
            shift = POSShift.open(other_profile, cls.other_user, 0)
            item = Item.objects.create(code="OTHER", name="خدمة أخرى", item_group=ItemGroup.objects.first(),
                uom=UOM.objects.first(), is_stock_item=False, selling_rate=999)
            cls.other_sale = checkout(shift, cls.other_user, [{"item": item.pk, "qty": 1}], paid_cash=1000)

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.shift = POSShift.open(self.profile, self.user, 0)
        self.sale = checkout(self.shift, self.user, [{"item": self.item.pk, "qty": 2}],
                             customer_id=self.customer.pk, paid_cash=500)
        self.client.force_login(self.user)

    def results(self, **params):
        response = self.client.get("/pos/api/invoices/", params)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        return response.json()["invoices"]

    def test_recent_and_partial_customer_phone_number_and_date(self):
        for params in ({}, {"q": "احمد نور"}, {"q": "٩٨٧٦"}, {"q": self.sale.name[-5:]},
                       {"date": self.sale.posting_date.isoformat()}):
            rows = self.results(**params)
            self.assertEqual([r["name"] for r in rows], [self.sale.name])
            self.assertEqual(rows[0]["grand_total"], str(self.sale.grand_total))
        self.assertContains(self.client.get(f"/pos/receipt/{self.sale.pk}/"), f'aria-label="{self.sale.name}"')
        self.assertEqual(self.results(q="غير موجود"), [])
        self.assertEqual(self.results(date="2000-01-01"), [])
        self.assertEqual(self.client.get("/pos/api/invoices/?date=invalid").status_code, 400)

    def test_other_tenant_draft_and_non_pos_are_excluded(self):
        SalesInvoice.objects.create(customer=self.customer, is_pos=True)
        self.assertEqual(len(self.results()), 1)
        self.assertEqual(self.results(q="999"), [])
        self.client.force_login(self.other_user)
        response = self.client.get("/pos/api/invoices/").json()
        self.assertEqual([r["grand_total"] for r in response["invoices"]], [str(self.other_sale.grand_total)])

    def test_return_status_uses_remaining_quantity_and_cancelled_returns(self):
        ret = pos_return(self.shift, self.user, self.sale.name, [{"item": self.item.pk, "qty": 1}])
        self.assertFalse(self.results()[0]["fully_returned"])
        ret2 = pos_return(self.shift, self.user, self.sale.name, [{"item": self.item.pk, "qty": 1}])
        self.assertTrue(self.results()[0]["fully_returned"])
        ret2.cancel()
        self.assertFalse(self.results()[0]["fully_returned"])

    def test_no_open_shift_cannot_search(self):
        POSShift.objects.filter(pk=self.shift.pk).update(status="closed")
        self.assertEqual(self.client.get("/pos/api/invoices/").status_code, 400)

    def test_result_limit_and_more(self):
        for n in range(31):
            SalesInvoice.objects.create(customer=self.customer, is_pos=True, docstatus=1, name=f"LOOKUP-{n}")
        response = self.client.get("/pos/api/invoices/").json()
        self.assertEqual(len(response["invoices"]), 30)
        self.assertTrue(response["has_more"])

    def test_branch_restrictions_are_applied_to_results(self):
        from accounts.models import CostCenter
        allowed = CostCenter.objects.create(name="فرع مسموح")
        hidden = CostCenter.objects.create(name="فرع آخر")
        membership = Membership.objects.get(company=self.company, user=self.user)
        membership.cost_centers.add(allowed)
        SalesInvoice.objects.filter(pk=self.sale.pk).update(cost_center=hidden)
        self.assertEqual(self.results(), [])
        SalesInvoice.objects.filter(pk=self.sale.pk).update(cost_center=allowed)
        self.assertEqual([row["name"] for row in self.results()], [self.sale.name])
