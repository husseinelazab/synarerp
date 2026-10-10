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
from selling.models import Customer, DeliveryNote, Quotation, SalesInvoice, SalesOrder
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse
from django.urls import reverse


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


class VATCategoryAndDebitNoteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الفئات الضريبية", "vatboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        group, uom = ItemGroup.objects.first(), UOM.objects.first()
        self.std = Item.objects.create(code="STD", name="خدمة عادية", item_group=group, uom=uom, is_stock_item=False)
        self.med = Item.objects.create(code="MED", name="جهاز طبي", item_group=group, uom=uom, is_stock_item=False,
                                       vat_category="Z", vat_exemption_reason="VATEX-SA-35")
        self.fin = Item.objects.create(code="FIN", name="عمولة تمويل", item_group=group, uom=uom, is_stock_item=False,
                                       vat_category="E", vat_exemption_reason="VATEX-SA-29")
        self.customer = Customer.objects.create(name="عميل الضريبة")

    def invoice(self, *lines, submit=True, **kwargs):
        inv = SalesInvoice.objects.create(customer=self.customer, update_stock=False, **kwargs)
        for item, rate in lines:
            inv.items.create(item=item, qty=1, rate=rate)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
        return inv

    def test_category_taken_from_items_and_no_tax(self):
        inv = self.invoice((self.med, 1000))
        self.assertEqual((inv.vat_category, inv.vat_exemption_reason, inv.tax_rate), ("Z", "VATEX-SA-35", 0))
        self.assertEqual((inv.tax_amount, inv.grand_total), (0, Decimal("1000.00")))
        self.assertFalse(GLEntry.objects.filter(voucher_id=inv.pk, account__name__contains="مخرجات").exists())

    def test_mixed_categories_must_be_split(self):
        with self.assertRaisesMessage(ValidationError, "افصلها"):
            self.invoice((self.std, 100), (self.med, 100))

    def test_reason_required_and_must_match(self):
        with self.assertRaisesMessage(ValidationError, "سبب الإعفاء"):
            self.invoice((self.std, 100), vat_category="E")
        with self.assertRaisesMessage(ValidationError, "لا يتوافق"):
            self.invoice((self.std, 100), vat_category="E", vat_exemption_reason="VATEX-SA-32")

    def test_back_to_standard_restores_rate(self):
        inv = self.invoice((self.std, 100), vat_category="O", vat_exemption_reason="VATEX-SA-OOS", submit=False)
        self.assertEqual(inv.tax_rate, 0)
        inv.vat_category, inv.vat_exemption_reason = "S", ""
        inv.calculate_totals()
        self.assertEqual((inv.tax_rate, inv.tax_amount), (15, Decimal("15.00")))

    def test_return_keeps_category(self):
        inv = self.invoice((self.fin, 500))
        ret = inv.make_return()
        self.assertEqual((ret.vat_category, ret.vat_exemption_reason, ret.tax_amount), ("E", "VATEX-SA-29", 0))
        ret.submit()

    def test_debit_note(self):
        inv = self.invoice((self.std, 1000))
        note = inv.make_debit_note(self.user)
        self.assertEqual((note.customer, note.tax_rate, note.update_stock), (self.customer, 15, False))
        note.items.create(item=self.std, qty=1, rate=200)
        note.remarks += " زيادة سعر متفق عليها"
        note.calculate_totals()
        note.save()
        note.submit()
        self.assertEqual((note.grand_total, note.outstanding_amount), (Decimal("230.00"), Decimal("230.00")))
        self.assertEqual(note.document_title, "إشعار مدين ضريبي")
        receivable = sum(g.debit - g.credit for g in GLEntry.objects.filter(account__number="1103", is_cancelled=False))
        self.assertEqual(receivable, Decimal("1380.00"))
        self.assertIn(note, inv.linked_documents)
        with self.assertRaisesMessage(ValidationError, "إشعارات مدينة"):
            inv.cancel()
        self.assertEqual(note.available_actions, [])
        # من صفحة الفاتورة
        response = self.client.post(reverse("doc_action", args=["sales-invoice", inv.pk, "make_debit_note"]))
        draft = SalesInvoice.objects.filter(is_debit_note=True, docstatus=0).get()
        self.assertRedirects(response, reverse("doc_edit", args=["sales-invoice", draft.pk]), fetch_redirect_response=False)

    def test_vat_report_lines(self):
        export = Item.objects.create(code="EXP", name="بضاعة مصدرة", item_group=ItemGroup.objects.first(),
                                     uom=UOM.objects.first(), is_stock_item=False, vat_category="Z",
                                     vat_exemption_reason="VATEX-SA-32")
        today = datetime.date.today()
        self.invoice((self.std, 1000), posting_date=today)
        self.invoice((self.med, 300), posting_date=today)
        self.invoice((export, 700), posting_date=today)
        self.invoice((self.fin, 50), posting_date=today)
        page = self.client.get(reverse("vat_report") + f"?from_date={today.replace(day=1)}&to_date={today}")
        rows = {r["no"]: (r["net"], r["tax"]) for r in page.context["sales_rows"]}
        self.assertEqual(rows[1], (Decimal("1000.00"), Decimal("150.00")))
        self.assertEqual(rows[3][0], Decimal("300.00"))
        self.assertEqual(rows[4][0], Decimal("700.00"))
        self.assertEqual(rows[5][0], Decimal("50.00"))
        self.assertEqual(page.context["output_tax"], Decimal("150.00"))


class FulfillmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الأذون", "dnboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        self.item = Item.objects.create(code="DSK", name="مكتب", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), selling_rate=500, buying_rate=300)
        self.customer = Customer.objects.create(name="عميل التسليم")
        self.supplier = Supplier.objects.create(name="مورد الاستلام")

    def gl(self, account_name):
        return sum(g.debit - g.credit for g in GLEntry.objects.filter(account__name=account_name, is_cancelled=False))

    def receive(self, qty=10, rate=300):
        po = PurchaseOrder.objects.create(supplier=self.supplier, warehouse=self.wh)
        po.items.create(item=self.item, qty=qty, rate=rate)
        po.submit()
        pr = po.make_fulfillment(self.user)
        pr.submit()
        return po, pr

    def test_purchase_receipt_then_invoice_clears_interim_account(self):
        po, pr = self.receive()
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 10)
        self.assertEqual(self.gl("المخزون"), Decimal("3000.00"))
        self.assertEqual(self.gl("بضائع مستلمة لم تصل فواتيرها"), Decimal("-3000.00"))
        po.refresh_from_db()
        self.assertEqual(po.per_delivered, 100)
        # فاتورة المورد بسعر أعلى قليلاً: الفرق لتسويات المخزون
        invoice = pr.make_invoice(self.user)
        self.assertFalse(invoice.update_stock)
        row = invoice.items.get()
        row.rate = 310
        row.save()
        invoice.calculate_totals()
        invoice.save()
        invoice.submit()
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 10)  # لم تتكرر
        self.assertEqual(self.gl("بضائع مستلمة لم تصل فواتيرها"), 0)
        self.assertEqual(self.gl("تسويات المخزون"), Decimal("100.00"))
        self.assertEqual(self.gl("الموردون"), Decimal("-3565.00"))  # 3100 + 15%
        pr.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual((pr.status, po.status), ("billed", CommercialStatus.BILLED))
        with self.assertRaisesMessage(ValidationError, "فُوتر"):
            pr.cancel()

    def test_delivery_note_then_invoice(self):
        self.receive()
        so = SalesOrder.objects.create(customer=self.customer, warehouse=self.wh)
        so.items.create(item=self.item, qty=6, rate=500)
        so.submit()
        self.assertIn("make_fulfillment", dict(so.available_actions))
        dn = so.make_fulfillment(self.user)
        row = dn.items.get()
        row.qty = 4  # تسليم جزئي
        row.save()
        dn.calculate_totals()
        dn.save()
        dn.submit()
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 6)
        self.assertEqual(self.gl("تكلفة البضاعة المباعة"), Decimal("1200.00"))  # 4 × 300 عند التسليم
        so.refresh_from_db()
        self.assertEqual(so.per_delivered, Decimal("66.67"))
        invoice = dn.make_invoice(self.user)
        self.assertEqual((invoice.update_stock, invoice.order, invoice.items.get().qty), (False, so, 4))
        invoice.submit()
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 6)
        self.assertEqual(self.gl("تكلفة البضاعة المباعة"), Decimal("1200.00"))  # لا تكلفة مكررة
        self.assertEqual(self.gl("المبيعات"), Decimal("-2000.00"))
        so.refresh_from_db()
        dn.refresh_from_db()
        self.assertEqual((so.status, dn.status), (CommercialStatus.PARTLY_BILLED, "billed"))
        # الفاتورة المباشرة من الأمر لا تُخرج المخزون مرة ثانية
        direct = so.make_invoice(self.user)
        self.assertFalse(direct.update_stock)
        direct.update_stock = True
        direct.save()
        with self.assertRaisesMessage(ValidationError, "إذن التسليم"):
            direct.submit()
        with self.assertRaisesMessage(ValidationError, "لا يمكن إلغاء الأمر"):
            so.cancel()
        invoice.cancel()
        with self.assertRaisesMessage(ValidationError, "أذون تسليم"):
            so.cancel()

    def test_cannot_deliver_more_than_ordered_or_stock(self):
        self.receive(qty=2)
        so = SalesOrder.objects.create(customer=self.customer, warehouse=self.wh)
        so.items.create(item=self.item, qty=1, rate=500)
        so.submit()
        dn = so.make_fulfillment(self.user)
        row = dn.items.get()
        row.qty = 3
        row.save()
        with self.assertRaisesMessage(ValidationError, "تتجاوز المتبقي"):
            dn.submit()
        free = DeliveryNote.objects.create(customer=self.customer, warehouse=self.wh)
        free.items.create(item=self.item, qty=5, rate=500)
        with self.assertRaisesMessage(ValidationError, "الرصيد غير كافٍ"):
            free.submit()

    def test_cancel_delivery_restores_stock_and_order(self):
        self.receive()
        dn = DeliveryNote.objects.create(customer=self.customer, warehouse=self.wh)
        dn.items.create(item=self.item, qty=3, rate=500)
        dn.calculate_totals()
        dn.save()
        dn.submit()
        dn.cancel()
        self.assertEqual(Bin.objects.get(item=self.item).actual_qty, 10)
        self.assertEqual(self.gl("تكلفة البضاعة المباعة"), 0)

    def test_pages(self):
        _, pr = self.receive()
        self.assertContains(self.client.get(pr.get_absolute_url()), "إنشاء فاتورة مشتريات")
        for slug in ("delivery-note", "purchase-receipt"):
            self.assertEqual(self.client.get(reverse("doc_list", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_new", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_import_template", args=[slug, "xlsx"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("doc_print", args=["purchase-receipt", pr.pk])).status_code, 200)
