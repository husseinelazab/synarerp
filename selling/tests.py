import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import GLEntry
from buying.models import PurchaseInvoice, PurchaseOrder, Supplier
from core.commercial import CommercialStatus
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, Quotation, SalesInvoice, SalesOrder
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse


class CommercialCycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الدورة التجارية", "trader")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        self.item = Item.objects.create(code="CH", name="كرسي", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), selling_rate=200, buying_rate=120)
        self.customer = Customer.objects.create(name="عميل الأوامر")
        self.supplier = Supplier.objects.create(name="مورد الأوامر")

    def quotation(self, qty=10, discount=0):
        q = Quotation.objects.create(customer=self.customer, valid_till=datetime.date.today() + datetime.timedelta(days=15),
                                     discount_amount=discount)
        q.items.create(item=self.item, qty=qty, rate=200)
        q.submit()
        return q

    def stock_in(self, qty):
        po = PurchaseOrder.objects.create(supplier=self.supplier, warehouse=self.wh)
        po.items.create(item=self.item, qty=qty, rate=120)
        po.submit()
        invoice = po.make_invoice()
        invoice.submit()
        return po

    def test_quotation_and_orders_post_nothing_and_need_no_fiscal_year(self):
        from core.models import FiscalYear
        FiscalYear.objects.update(is_closed=True)
        q = self.quotation()
        so = q.make_sales_order()
        so.submit()
        self.assertEqual(GLEntry.objects.count(), 0)
        self.assertEqual(Bin.objects.count(), 0)
        q.refresh_from_db()
        self.assertEqual((q.status, so.status), (CommercialStatus.ORDERED, CommercialStatus.TO_BILL))
        self.assertEqual(so.quotation, q)

    def test_full_cycle_with_partial_billing(self):
        self.stock_in(20)
        q = self.quotation(qty=10, discount=100)
        so = q.make_sales_order()
        so.warehouse = self.wh
        so.save()
        so.submit()
        self.assertEqual(so.grand_total, Decimal("2185.00"))  # (2000 - 100) × 1.15
        first = so.make_invoice()
        row = first.items.get()
        self.assertEqual((row.qty, row.order_item.parent, first.order), (10, so, so))
        row.qty = 4
        row.save()
        first.calculate_totals()
        first.save()
        self.assertEqual(first.discount_amount, Decimal("40.00"))  # الخصم يُنقل بنسبة ما فُوتر
        first.submit()
        so.refresh_from_db()
        self.assertEqual((so.status, so.per_billed), (CommercialStatus.PARTLY_BILLED, Decimal("40.00")))
        second = so.make_invoice()
        self.assertEqual(second.items.get().qty, 6)                  # المتبقي فقط
        second.submit()
        so.refresh_from_db()
        self.assertEqual((so.status, so.per_billed), (CommercialStatus.BILLED, Decimal("100.00")))
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 10)
        total = SalesInvoice.objects.filter(order=so, docstatus=1).aggregate(v=Sum("grand_total"))["v"]
        self.assertEqual(total, so.grand_total)
        with self.assertRaises(ValidationError):
            so.make_invoice()                                       # لا متبقي للفوترة
        with self.assertRaises(ValidationError):
            so.cancel()                                             # عليه فواتير معتمدة
        second.cancel()
        so.refresh_from_db()
        self.assertEqual(so.status, CommercialStatus.PARTLY_BILLED)

    def test_over_billing_rejected(self):
        self.stock_in(20)
        so = self.quotation(qty=5).make_sales_order()
        so.warehouse = self.wh
        so.save()
        so.submit()
        invoice = so.make_invoice()
        row = invoice.items.get()
        row.qty = 7
        row.save()
        with self.assertRaises(ValidationError):
            invoice.submit()
        self.assertEqual(SalesOrder.objects.get().status, CommercialStatus.TO_BILL)

    def test_close_and_reopen_order(self):
        so = self.quotation().make_sales_order()
        so.submit()
        so.close_order()
        with self.assertRaises(ValidationError):
            so.make_invoice()
        so.reopen_order()
        self.assertEqual(so.status, CommercialStatus.TO_BILL)

    def test_quotation_direct_to_invoice_and_return_does_not_touch_billing(self):
        self.stock_in(5)
        q = self.quotation(qty=2)
        invoice = q.make_invoice()
        self.assertEqual(invoice.warehouse, self.wh)  # المستودع الافتراضي عند التحويل
        invoice.submit()
        q.refresh_from_db()
        self.assertEqual(q.status, CommercialStatus.ORDERED)
        invoice.make_return().submit()
        q.refresh_from_db()
        self.assertEqual(q.status, CommercialStatus.ORDERED)

    def test_purchase_order_to_invoice_updates_stock(self):
        po = self.stock_in(8)
        po.refresh_from_db()
        self.assertEqual(po.status, CommercialStatus.BILLED)
        invoice = PurchaseInvoice.objects.get(order=po)
        self.assertEqual(invoice.warehouse, self.wh)
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 8)
        bad = PurchaseOrder.objects.create(supplier=self.supplier, discount_amount=10)
        bad.items.create(item=self.item, qty=1, rate=100)
        with self.assertRaises(ValidationError):
            bad.submit()

    def test_ui_flow_buttons_print_and_import(self):
        q = self.quotation(qty=3)
        page = self.client.get(f"/app/quotation/{q.pk}/")
        self.assertContains(page, "إنشاء أمر بيع")
        self.assertContains(page, "لا يُنشئ قيوداً محاسبية")
        response = self.client.post(f"/app/quotation/{q.pk}/make_sales_order/")
        so = SalesOrder.objects.get()
        self.assertRedirects(response, f"/app/sales-order/{so.pk}/edit/")
        self.client.post(f"/app/sales-order/{so.pk}/submit/")
        so.refresh_from_db()
        self.assertTrue(so.is_submitted)
        page = self.client.get(f"/app/sales-order/{so.pk}/")
        self.assertContains(page, "إنشاء فاتورة مبيعات")
        self.assertContains(page, q.name)                           # المستندات المرتبطة
        self.assertContains(self.client.get(f"/app/quotation/{q.pk}/print/"), "عرض سعر")
        self.assertEqual(self.client.post(f"/app/quotation/{q.pk}/make_sales_order/").status_code, 302)
        self.assertEqual(SalesOrder.objects.count(), 1)             # لا يُحوّل عرض محوّل مرة أخرى
        for slug in ("quotation", "sales-order", "purchase-order"):
            self.assertEqual(self.client.get(f"/app/{slug}/").status_code, 200)
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200)
            self.assertEqual(self.client.get(f"/app/{slug}/template.xlsx").status_code, 200)
        self.assertContains(self.client.get("/"), "بانتظار الفوترة")
