from decimal import Decimal

from django.test import TestCase

from buying.models import MaterialRequest, PurchaseOrder, Supplier, SupplierQuotation
from core.tenancy import tenant_context
from saas.tests import make_company
from stock.models import UOM, Item, ItemGroup, OpeningStock, Warehouse


class MaterialRequestTests(TestCase):
    """طلب المواد ← عروض أسعار الموردين ومقارنتها ← أمر شراء، أو تحويل مخزني."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة المشتريات", "procure")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.item = Item.objects.create(code="MR1", name="ورق تصوير", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), buying_rate=20)
        self.main, self.branch = Warehouse.objects.all()[:2]
        self.s1 = Supplier.objects.create(name="مورد أ")
        self.s2 = Supplier.objects.create(name="مورد ب")

    def request(self, purpose="purchase", qty=100):
        mr = MaterialRequest.objects.create(purpose=purpose, warehouse=self.branch,
                                            from_warehouse=self.main if purpose == "transfer" else None)
        mr.items.create(item=self.item, qty=qty)
        mr.submit()
        return mr

    def quote(self, mr_or_quote, supplier, rate):
        q = mr_or_quote.make_supplier_quotation() if isinstance(mr_or_quote, MaterialRequest) \
            else mr_or_quote.copy_for_supplier()
        q.supplier = supplier
        q.tax_rate = 0
        q.save()
        q.items.update(rate=rate)
        q.calculate_totals()
        q.save()
        q.submit()
        return q

    def test_quotations_compared_and_best_ordered(self):
        mr = self.request()
        q1 = self.quote(mr, self.s1, 22)
        q2 = self.quote(q1, self.s2, 19)
        comparison = mr.quotation_comparison()[0]
        best = [o for o in comparison["offers"] if o["best"]][0]
        self.assertEqual((best["supplier"], best["quote"]), (self.s2, q2))
        order = q2.make_purchase_order()
        self.assertEqual(order.supplier, self.s2)
        self.assertEqual(order.items.get().rate, Decimal("19"))
        order.submit()
        mr.refresh_from_db()
        q2.refresh_from_db()
        self.assertEqual((mr.status, mr.per_ordered), ("ordered", 100))
        self.assertEqual(q2.status, "ordered")
        order.cancel()
        mr.refresh_from_db()
        self.assertEqual(mr.status, "pending")

    def test_partial_purchase_order(self):
        mr = self.request()
        order = mr.make_purchase_order()
        order.items.update(qty=40)
        order.calculate_totals()
        order.save()
        order.submit()
        mr.refresh_from_db()
        self.assertEqual((mr.status, mr.per_ordered), ("partial", 40))
        self.assertEqual(list(mr.remaining().values()), [Decimal("60")])

    def test_transfer_request(self):
        stock = OpeningStock.objects.create(warehouse=self.main)
        stock.items.create(item=self.item, qty=500, rate=20)
        stock.submit()
        mr = self.request("transfer", qty=30)
        self.assertEqual(mr.available_actions[0][0], "make_transfer")
        entry = mr.make_transfer()
        entry.submit()
        mr.refresh_from_db()
        self.assertEqual(mr.status, "ordered")
        entry.cancel()
        mr.refresh_from_db()
        self.assertEqual(mr.status, "pending")

    def test_pages(self):
        mr = self.request()
        self.quote(mr, self.s1, 21)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(mr.get_absolute_url()), "مقارنة عروض الموردين")
        for slug in ("material-request", "supplier-quotation"):
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200)
        response = self.client.post(f"/app/material-request/{mr.pk}/make_supplier_quotation/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SupplierQuotation.objects.count(), 2)
        self.assertFalse(PurchaseOrder.objects.exists())
