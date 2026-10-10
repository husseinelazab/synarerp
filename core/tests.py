from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase

from accounts.models import GLEntry, JournalEntry, PaymentEntry
from buying.models import PurchaseInvoice, Supplier
from core.models import Company, DocStatus
from core.registry import REGISTRY
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Bin, Item, ItemGroup, StockEntry, Warehouse


def gl_balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class ERPFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", company="شركة الاختبار", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.get_default()
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.item = Item.objects.create(
            code="T1", name="صنف اختبار", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
            buying_rate=100, selling_rate=150,
        )
        cls.supplier = Supplier.objects.create(name="مورد")
        cls.customer = Customer.objects.create(name="عميل")

    def purchase(self, qty, rate):
        inv = PurchaseInvoice.objects.create(supplier=self.supplier, warehouse=self.wh)
        inv.items.create(item=self.item, qty=qty, rate=rate)
        inv.submit()
        return inv

    def sell(self, qty, rate, discount=0):
        inv = SalesInvoice.objects.create(customer=self.customer, warehouse=self.wh, discount_amount=discount)
        inv.items.create(item=self.item, qty=qty, rate=rate)
        inv.submit()
        return inv

    def bin(self):
        return Bin.objects.get(item=self.item, warehouse=self.wh)

    def test_naming_series(self):
        inv = self.purchase(1, 10)
        self.assertRegex(inv.name, r"^PINV-\d{4}-00001$")

    def test_purchase_posts_stock_vat_and_payable(self):
        inv = self.purchase(10, 100)
        self.assertEqual(inv.grand_total, Decimal("1150.00"))
        self.assertEqual(self.bin().actual_qty, 10)
        self.assertEqual(gl_balance(account=self.company.default_stock_account), Decimal("1000"))
        self.assertEqual(gl_balance(account=self.company.default_input_tax_account), Decimal("150"))
        self.assertEqual(gl_balance(supplier=self.supplier), Decimal("-1150"))
        self.assertEqual(gl_balance(), 0)

    def test_moving_average_and_cogs(self):
        self.purchase(10, 100)
        self.purchase(10, 200)
        self.assertEqual(self.bin().valuation_rate, Decimal("150"))
        inv = self.sell(5, 300, discount=100)
        self.assertEqual(inv.net_total, Decimal("1400.00"))
        self.assertEqual(inv.tax_amount, Decimal("210.00"))
        self.assertEqual(inv.grand_total, Decimal("1610.00"))
        self.assertEqual(gl_balance(account=self.company.default_cogs_account), Decimal("750"))
        self.assertEqual(gl_balance(account=self.company.default_stock_account), Decimal("2250"))
        self.assertEqual(self.bin().stock_value, Decimal("2250"))
        self.assertEqual(gl_balance(customer=self.customer), Decimal("1610"))
        self.assertEqual(gl_balance(), 0)

    def test_negative_stock_blocked(self):
        self.purchase(2, 100)
        with self.assertRaises(ValidationError):
            self.sell(3, 150)
        self.assertEqual(self.bin().actual_qty, 2)
        self.assertFalse(SalesInvoice.objects.filter(docstatus=DocStatus.SUBMITTED).exists())

    def test_cancel_reverses_everything(self):
        self.purchase(10, 100)
        inv = self.sell(4, 150)
        inv.cancel()
        self.assertEqual(self.bin().actual_qty, 10)
        self.assertEqual(self.bin().stock_value, Decimal("1000"))
        self.assertEqual(gl_balance(customer=self.customer), 0)
        self.assertEqual(gl_balance(), 0)

    def test_payment_updates_outstanding_and_blocks_cancel(self):
        self.purchase(10, 100)
        inv = self.sell(2, 150)
        pay = PaymentEntry.objects.create(
            payment_type="receive", customer=self.customer, account=self.company.default_cash_account,
            amount=Decimal("100"), sales_invoice=inv,
        )
        pay.submit()
        inv.refresh_from_db()
        self.assertEqual(inv.outstanding_amount, Decimal("245.00"))
        with self.assertRaises(ValidationError):
            inv.cancel()
        pay.cancel()
        inv.refresh_from_db()
        self.assertEqual(inv.outstanding_amount, inv.grand_total)

    def test_overpayment_rejected(self):
        self.purchase(10, 100)
        inv = self.sell(1, 100)
        pay = PaymentEntry.objects.create(
            payment_type="receive", customer=self.customer, account=self.company.default_cash_account,
            amount=Decimal("1000"), sales_invoice=inv,
        )
        with self.assertRaises(ValidationError):
            pay.submit()

    def test_unbalanced_journal_rejected(self):
        je = JournalEntry.objects.create()
        je.accounts.create(account=self.company.default_cash_account, debit=100)
        je.accounts.create(account=self.company.default_income_account, credit=90)
        with self.assertRaises(ValidationError):
            je.submit()
        je.refresh_from_db()
        self.assertEqual(je.docstatus, DocStatus.DRAFT)

    def test_journal_requires_party_on_receivable(self):
        je = JournalEntry.objects.create()
        je.accounts.create(account=self.company.default_receivable_account, debit=100)
        je.accounts.create(account=self.company.default_income_account, credit=100)
        with self.assertRaises(ValidationError):
            je.submit()

    def test_stock_entry_transfer_and_receipt(self):
        other = Warehouse.objects.get(name="مستودع الفرع")
        receipt = StockEntry.objects.create(purpose="receipt", to_warehouse=self.wh)
        receipt.items.create(item=self.item, qty=5, rate=20)
        receipt.submit()
        self.assertEqual(gl_balance(account=self.company.default_stock_account), Decimal("100"))
        transfer = StockEntry.objects.create(purpose="transfer", from_warehouse=self.wh, to_warehouse=other)
        transfer.items.create(item=self.item, qty=2)
        transfer.submit()
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=other).stock_value, Decimal("40"))
        self.assertEqual(self.bin().actual_qty, 3)
        self.assertEqual(gl_balance(), 0)


class ViewSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, verbosity=0, stdout=open("/dev/null", "w"))
        cls.user = User.objects.create_superuser("tester", "", "pass12345")

    def setUp(self):
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_all_pages_render(self):
        urls = [
            "/", "/reports/accounts/trial-balance/", "/reports/accounts/general-ledger/",
            "/reports/accounts/profit-and-loss/", "/reports/accounts/balance-sheet/", "/reports/accounts/vat/",
            "/reports/accounts/balances/customer/", "/reports/accounts/balances/supplier/",
            "/reports/accounts/chart-of-accounts/", "/reports/stock/balance/", "/reports/stock/ledger/",
        ]
        for slug, dt in REGISTRY.items():
            urls += [f"/app/{slug}/", f"/app/{slug}/new/", f"/app/{slug}/?q=a"]
            obj = dt.model.objects.first()
            if obj:
                urls += [f"/app/{slug}/{obj.pk}/", f"/app/{slug}/{obj.pk}/edit/"]
                if dt.is_submittable:
                    urls.append(f"/app/{slug}/{obj.pk}/print/")
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 200)

    def test_balance_sheet_balanced(self):
        self.assertContains(self.client.get("/reports/accounts/balance-sheet/"), "الميزانية متوازنة")

    def test_create_and_submit_invoice_via_form(self):
        customer = Customer.objects.first()
        item = Item.objects.get(code="MOU-001")
        wh = Warehouse.objects.get(name="المستودع الرئيسي")
        data = {
            "customer": customer.pk, "posting_date": "2026-05-01", "update_stock": "on", "warehouse": wh.pk,
            "tax_rate": "15", "discount_amount": "0", "remarks": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-item": item.pk, "items-0-qty": "3", "items-0-rate": "75", "_submit": "1",
        }
        response = self.client.post("/app/sales-invoice/new/", data, follow=True)
        self.assertEqual(response.status_code, 200)
        inv = SalesInvoice.objects.get(posting_date="2026-05-01")
        self.assertEqual(inv.docstatus, DocStatus.SUBMITTED)
        self.assertEqual(inv.grand_total, Decimal("258.75"))


class ReturnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", company="شركة الاختبار", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.get_default()
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.item = Item.objects.create(
            code="R1", name="صنف", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
            buying_rate=100, selling_rate=200,
        )
        cls.supplier = Supplier.objects.create(name="مورد")
        cls.customer = Customer.objects.create(name="عميل")

    def purchase(self, qty, rate):
        inv = PurchaseInvoice.objects.create(supplier=self.supplier, warehouse=self.wh)
        inv.items.create(item=self.item, qty=qty, rate=rate)
        inv.submit()
        return inv

    def sale(self, qty, rate=200, discount=0):
        inv = SalesInvoice.objects.create(customer=self.customer, warehouse=self.wh, discount_amount=discount)
        inv.items.create(item=self.item, qty=qty, rate=rate)
        inv.submit()
        return inv

    def bin(self):
        return Bin.objects.get(item=self.item, warehouse=self.wh)

    def test_sales_return_reverses_at_original_cost(self):
        self.purchase(10, 100)
        inv = self.sale(4)
        self.purchase(10, 160)  # التكلفة الحالية تتغير بعد البيع
        ret = inv.make_return(quantities={self.item.id: 1})
        ret.submit()
        self.assertTrue(ret.name.startswith("SRET-"))
        self.assertEqual(ret.grand_total, Decimal("-230.00"))
        # تعود الوحدة بتكلفتها الأصلية 100 وليس بالمتوسط الحالي
        self.assertEqual(gl_balance(account=self.company.default_cogs_account), Decimal("300"))
        self.assertEqual(self.bin().actual_qty, 17)
        self.assertEqual(gl_balance(account=self.company.default_stock_account), self.bin().stock_value)
        self.assertEqual(gl_balance(account=self.company.default_output_tax_account), Decimal("-90"))
        inv.refresh_from_db()
        self.assertEqual(inv.outstanding_amount, Decimal("690.00"))
        self.assertEqual(ret.outstanding_amount, 0)
        self.assertEqual(gl_balance(customer=self.customer), Decimal("690"))
        self.assertEqual(gl_balance(), 0)

    def test_return_quantity_cannot_exceed_sold(self):
        self.purchase(10, 100)
        inv = self.sale(3)
        inv.make_return(quantities={self.item.id: 2}).submit()
        self.assertEqual(inv.returnable_quantities(), {self.item.id: Decimal("1")})
        over = inv.make_return(quantities={self.item.id: 2})
        with self.assertRaises(ValidationError):
            over.submit()
        inv.make_return(quantities={self.item.id: 1}).submit()
        with self.assertRaises(ValidationError):
            inv.make_return()

    def test_return_of_paid_invoice_creates_refund_due_and_refund_payment(self):
        self.purchase(10, 100)
        inv = self.sale(2)  # 460
        PaymentEntry.objects.create(payment_type="receive", customer=self.customer, amount=Decimal("460"),
                                    account=self.company.default_cash_account, sales_invoice=inv).submit()
        ret = inv.make_return(quantities={self.item.id: 1})
        ret.submit()
        self.assertEqual(ret.outstanding_amount, Decimal("-230.00"))
        self.assertEqual(gl_balance(customer=self.customer), Decimal("-230"))
        refund = PaymentEntry.objects.create(payment_type="pay", customer=self.customer, amount=Decimal("230"),
                                             account=self.company.default_cash_account, sales_invoice=ret)
        refund.submit()
        ret.refresh_from_db()
        self.assertEqual(ret.outstanding_amount, 0)
        self.assertEqual(gl_balance(customer=self.customer), 0)
        too_much = PaymentEntry.objects.create(payment_type="pay", customer=self.customer, amount=Decimal("1"),
                                               account=self.company.default_cash_account, sales_invoice=ret)
        with self.assertRaises(ValidationError):
            too_much.submit()

    def test_discount_is_returned_proportionally(self):
        self.purchase(10, 100)
        inv = self.sale(4, discount=80)  # 800 - 80 = 720 + 108 ضريبة
        ret = inv.make_return(quantities={self.item.id: 1})
        self.assertEqual(ret.discount_amount, Decimal("-20.00"))
        ret.submit()
        self.assertEqual(ret.grand_total, Decimal("-207.00"))
        self.assertEqual(gl_balance(), 0)

    def test_return_prices_cannot_be_changed(self):
        self.purchase(10, 100)
        inv = self.sale(2)
        ret = inv.make_return()
        row = ret.items.get()
        row.rate = 1
        row.save()
        ret.submit()
        self.assertEqual(ret.items.get().rate, Decimal("200"))

    def test_cancel_return_restores_original(self):
        self.purchase(10, 100)
        inv = self.sale(2)
        ret = inv.make_return()
        ret.submit()
        with self.assertRaises(ValidationError):
            inv.cancel()  # لا يُلغى الأصل قبل إلغاء المرتجع
        ret.cancel()
        inv.refresh_from_db()
        self.assertEqual(inv.outstanding_amount, inv.grand_total)
        self.assertEqual(self.bin().actual_qty, 8)
        inv.cancel()
        self.assertEqual(gl_balance(), 0)

    def test_purchase_return_posts_valuation_difference(self):
        self.purchase(10, 100)
        second = self.purchase(10, 200)  # المتوسط 150
        ret = second.make_return(quantities={self.item.id: 2})
        ret.submit()
        self.assertTrue(ret.name.startswith("PRET-"))
        self.assertEqual(ret.grand_total, Decimal("-460.00"))
        self.assertEqual(self.bin().actual_qty, 18)
        self.assertEqual(gl_balance(account=self.company.default_stock_account), self.bin().stock_value)
        self.assertEqual(gl_balance(account=self.company.default_stock_adjustment_account), Decimal("-100"))
        self.assertEqual(gl_balance(supplier=self.supplier), Decimal("-2990"))  # 1150 + 2300 - 460
        self.assertEqual(gl_balance(), 0)

    def test_return_ui_flow(self):
        self.purchase(10, 100)
        inv = self.sale(2)
        user = User.objects.create_superuser("u", "", "pass12345")
        self.client.force_login(user)
        self.assertContains(self.client.get(f"/app/sales-invoice/{inv.pk}/"), "إنشاء مرتجع")
        response = self.client.post(f"/app/sales-invoice/{inv.pk}/return/")
        ret = SalesInvoice.objects.get(is_return=True)
        self.assertRedirects(response, f"/app/sales-invoice/{ret.pk}/edit/")
        self.assertContains(self.client.get(f"/app/sales-invoice/{ret.pk}/edit/"), "مرتجع من الفاتورة")
        self.client.post(f"/app/sales-invoice/{ret.pk}/submit/")
        ret.refresh_from_db()
        self.assertTrue(ret.is_submitted)
        self.assertContains(self.client.get(f"/app/sales-invoice/{ret.pk}/print/"), "إشعار دائن")
        self.assertNotContains(self.client.get(f"/app/sales-invoice/{inv.pk}/"), "إنشاء مرتجع")
