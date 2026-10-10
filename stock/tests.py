import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import GLEntry
from core.tenancy import tenant_context
from saas.tests import make_company
from stock.models import UOM, Bin, Item, ItemGroup, OpeningStock, StockCount, Warehouse


class StockCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الجرد", "countboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        group, uom = ItemGroup.objects.first(), UOM.objects.first()
        self.a = Item.objects.create(code="A", name="أقلام", item_group=group, uom=uom, buying_rate=2)
        self.b = Item.objects.create(code="B", name="دفاتر", item_group=group, uom=uom, buying_rate=10)
        self.c = Item.objects.create(code="C", name="مساطر", item_group=group, uom=uom, buying_rate=3)
        opening = OpeningStock.objects.create(warehouse=self.wh)
        opening.items.create(item=self.a, qty=100, rate=2)
        opening.items.create(item=self.b, qty=50, rate=10)
        opening.calculate_totals()
        opening.save()
        opening.submit()

    def count(self, **counted):
        sc = StockCount.objects.create(warehouse=self.wh)
        sc.calculate_totals()
        sc.save()
        for row in sc.items.select_related("item"):
            if row.item.code in counted:
                row.counted_qty = counted[row.item.code]
                row.save()
        sc.calculate_totals()
        sc.save()
        return sc

    def test_fetches_items_with_book_quantities(self):
        sc = self.count()
        self.assertEqual({r.item.code: r.counted_qty for r in sc.items.all()}, {"A": 100, "B": 50})
        with self.assertRaisesMessage(ValidationError, "لا توجد فروقات"):
            sc.submit()

    def test_shortage_and_surplus_post_to_adjustment(self):
        sc = self.count(A=90, B=55)
        rows = {r.item.code: r for r in sc.items.all()}
        self.assertEqual((rows["A"].difference_qty, rows["A"].difference_value), (Decimal("-10"), Decimal("-20.00")))
        self.assertEqual((rows["B"].difference_qty, rows["B"].difference_value), (Decimal("5"), Decimal("50.00")))
        sc.submit()
        self.assertEqual(Bin.objects.get(item=self.a).actual_qty, 90)
        self.assertEqual(Bin.objects.get(item=self.b).actual_qty, 55)
        gl = {g.account.number: g.debit - g.credit for g in GLEntry.objects.filter(voucher_type="stock-count")}
        self.assertEqual(gl, {"1104": Decimal("30.00"), "5102": Decimal("-30.00")})
        sc.cancel()
        self.assertEqual(Bin.objects.get(item=self.a).actual_qty, 100)

    def test_item_group_count_includes_unrecorded_items(self):
        sc = StockCount.objects.create(warehouse=self.wh, item_group=ItemGroup.objects.first())
        sc.calculate_totals()
        sc.save()
        self.assertEqual(sc.items.count(), 3)  # يشمل «مساطر» بلا رصيد ليُعدّ إن وُجد
        row = sc.items.get(item=self.c)
        row.counted_qty = 20
        row.save()
        sc.calculate_totals()
        sc.save()
        sc.submit()
        b = Bin.objects.get(item=self.c)
        self.assertEqual((b.actual_qty, b.stock_value), (20, Decimal("60.00")))  # بسعر الشراء

    def test_form_and_pages(self):
        response = self.client.post(reverse("doc_new", args=["stock-count"]), {
            "warehouse": self.wh.pk, "posting_date": str(datetime.date.today()), "remarks": "",
            "items-TOTAL_FORMS": 0, "items-INITIAL_FORMS": 0, "items-MIN_NUM_FORMS": 0, "items-MAX_NUM_FORMS": 1000,
        })
        sc = StockCount.objects.get()
        self.assertRedirects(response, sc.get_absolute_url())
        self.assertContains(self.client.get(sc.get_absolute_url()), "الكمية الدفترية")
        self.assertEqual(self.client.get(reverse("doc_print", args=["stock-count", sc.pk])).status_code, 200)
