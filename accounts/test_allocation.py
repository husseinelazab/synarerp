import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import GLEntry, PaymentEntry, PaymentReconciliation, PaymentType
from buying.models import PurchaseInvoice, Supplier
from core.models import Currency, ExchangeRate
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


def party_balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class AllocationTests(TestCase):
    """توزيع السند الواحد على عدة فواتير، والدفعات المقدمة، وتسوية الدفعات."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة التخصيص", "allocator")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.customer = Customer.objects.create(name="عميل التخصيص")
        self.item = Item.objects.create(code="AL1", name="خدمة", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), is_stock_item=False, selling_rate=100,
                                        vat_category="Z", vat_exemption_reason="VATEX-SA-32")
        self.today = datetime.date.today()

    def invoice(self, amount, days_ago=0, customer=None, **extra):
        inv = SalesInvoice.objects.create(customer=customer or self.customer, tax_rate=0, vat_category="Z",
                                          vat_exemption_reason="VATEX-SA-32", update_stock=False,
                                          posting_date=self.today - datetime.timedelta(days=days_ago),
                                          due_date=self.today - datetime.timedelta(days=days_ago), **extra)
        inv.items.create(item=self.item, qty=1, rate=amount)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def payment(self, amount, submit=True, **extra):
        pay = PaymentEntry.objects.create(payment_type=PaymentType.RECEIVE, customer=self.customer, amount=amount,
                                          account=self.company.default_cash_account, **extra)
        if submit:
            pay.calculate_totals()
            pay.save()
            pay.submit()
        return pay

    def refresh(self, *docs):
        for doc in docs:
            doc.refresh_from_db()

    def test_one_receipt_pays_several_invoices(self):
        a, b, c = self.invoice(300), self.invoice(200), self.invoice(500)
        pay = self.payment(700, submit=False)
        pay.references.create(sales_invoice=a, allocated_amount=300)
        pay.references.create(sales_invoice=b, allocated_amount=200)
        pay.references.create(sales_invoice=c, allocated_amount=150)
        pay.calculate_totals()
        pay.save()
        pay.submit()
        self.refresh(a, b, c, pay)
        self.assertEqual((a.outstanding_amount, b.outstanding_amount, c.outstanding_amount), (0, 0, Decimal("350")))
        self.assertEqual(pay.allocated_amount, Decimal("650"))
        self.assertEqual(pay.unallocated_amount, Decimal("50"))
        self.assertEqual(party_balance(customer=self.customer), Decimal("300"))  # 1000 - 700
        with self.assertRaises(ValidationError):  # لا تُلغى فاتورة مسددة بسند
            a.cancel()
        pay.cancel()
        self.refresh(a, b, c)
        self.assertEqual((a.outstanding_amount, b.outstanding_amount, c.outstanding_amount),
                         (Decimal("300"), Decimal("200"), Decimal("500")))

    def test_over_allocation_rejected(self):
        a = self.invoice(300)
        pay = self.payment(200, submit=False)
        pay.references.create(sales_invoice=a, allocated_amount=250)
        with self.assertRaises(ValidationError):
            pay.submit()
        pay.references.all().delete()
        pay.references.create(sales_invoice=a, allocated_amount=200)
        pay.sales_invoice = a
        with self.assertRaises(ValidationError):  # الفاتورة في الرأس والجدول معاً
            pay.submit()

    def test_allocation_beyond_outstanding_rejected(self):
        a = self.invoice(100)
        pay = self.payment(500, submit=False)
        pay.references.create(sales_invoice=a, allocated_amount=150)
        with self.assertRaises(ValidationError):
            pay.submit()

    def test_auto_allocate_oldest_first(self):
        new, old = self.invoice(400, days_ago=1), self.invoice(300, days_ago=40)
        pay = self.payment(500, submit=False)
        self.assertEqual(pay.draft_actions[0][0], "allocate_oldest")
        pay.allocate_oldest()
        allocated = {r.sales_invoice_id: r.allocated_amount for r in pay.references.all()}
        self.assertEqual(allocated, {old.pk: Decimal("300"), new.pk: Decimal("200")})
        pay.submit()
        self.refresh(new, old)
        self.assertEqual((old.outstanding_amount, new.outstanding_amount), (0, Decimal("200")))

    def test_header_invoice_still_works(self):
        a = self.invoice(300)
        pay = self.payment(120, sales_invoice=a)
        self.refresh(a, pay)
        self.assertEqual(a.outstanding_amount, Decimal("180"))
        self.assertEqual(pay.unallocated_amount, 0)
        self.assertEqual([row["amount"] for row in a.payment_rows()], [Decimal("120")])

    def test_reconcile_advance_and_credit_note(self):
        advance = self.payment(250)
        self.assertEqual(advance.unallocated_amount, Decimal("250"))
        a, b = self.invoice(200, days_ago=10), self.invoice(300)
        ret = a.make_return()
        ret.submit()  # إرجاع كامل الفاتورة a: يُخصم منها، والفاتورة b تبقى مستحقة
        self.refresh(a)
        self.assertEqual(a.outstanding_amount, 0)
        # إشعار دائن ثانٍ لا يخصم من فاتورته (فاتورة مسددة بالكامل) فيبقى رصيداً غير مخصص
        c = self.invoice(100)
        self.payment(100, sales_invoice=c)
        ret_c = c.make_return()
        ret_c.submit()
        self.refresh(ret_c)
        self.assertEqual(ret_c.outstanding_amount, Decimal("-100"))

        self.client.force_login(self.user)
        page = self.client.get(f"/reports/accounts/reconcile/?party_type=customer&party={self.customer.pk}")
        self.assertEqual(page.status_code, 200)
        keys = {s["key"] for s in page.context["sources"]}
        self.assertEqual(keys, {f"p-{advance.pk}", f"r-{ret_c.pk}"})
        lines = page.context["lines"]
        self.assertEqual(sum(line["amount"] for line in lines), Decimal("300"))  # الفاتورة b كاملة
        response = self.client.post(page.request["PATH_INFO"] + f"?party_type=customer&party={self.customer.pk}", {
            "source": [f"p-{advance.pk}", f"r-{ret_c.pk}"], "invoice": [b.pk, b.pk], "amount": ["250", "50"]})
        doc = PaymentReconciliation.objects.get()
        self.assertRedirects(response, doc.get_absolute_url(), fetch_redirect_response=False)
        self.assertTrue(doc.is_submitted)
        self.refresh(b, advance, ret_c)
        self.assertEqual(b.outstanding_amount, 0)
        self.assertEqual(advance.unallocated_amount, 0)
        self.assertEqual(ret_c.outstanding_amount, Decimal("-50"))
        self.assertFalse(GLEntry.objects.filter(voucher_type="payment-reconciliation").exists())  # بلا فرق صرف
        with self.assertRaises(ValidationError):
            advance.cancel()
        doc.cancel()
        self.refresh(b, advance, ret_c)
        self.assertEqual((b.outstanding_amount, advance.unallocated_amount, ret_c.outstanding_amount),
                         (Decimal("300"), Decimal("250"), Decimal("-100")))

    def test_reconciliation_cannot_exceed_available(self):
        advance = self.payment(100)
        b = self.invoice(500)
        doc = PaymentReconciliation.objects.create(party_type="customer", customer=self.customer)
        doc.lines.create(payment=advance, sales_invoice=b, amount=150)
        with self.assertRaises(ValidationError):
            doc.submit()

    def test_supplier_advance_reconciled(self):
        supplier = Supplier.objects.create(name="مورد التخصيص")
        pinv = PurchaseInvoice.objects.create(supplier=supplier, tax_rate=0, vat_category="Z",
                                              vat_exemption_reason="VATEX-SA-32", update_stock=False)
        pinv.items.create(item=self.item, qty=1, rate=400)
        pinv.calculate_totals()
        pinv.save()
        pinv.submit()
        advance = PaymentEntry.objects.create(payment_type=PaymentType.PAY, supplier=supplier, amount=400,
                                              account=self.company.default_cash_account)
        advance.calculate_totals()
        advance.save()
        advance.submit()
        doc = PaymentReconciliation.objects.create(party_type="supplier", supplier=supplier)
        doc.lines.create(payment=advance, purchase_invoice=pinv, amount=400)
        doc.submit()
        pinv.refresh_from_db()
        self.assertEqual(pinv.outstanding_amount, 0)

    def test_foreign_currency_reconciliation_posts_exchange_difference(self):
        usd = Currency.objects.get(code="USD")
        ExchangeRate.objects.create(currency=usd, date=self.today - datetime.timedelta(days=30), rate=Decimal("3.75"))
        advance = self.payment(100, currency=usd, exchange_rate=Decimal("3.70"))
        inv = self.invoice(100, currency=usd, exchange_rate=Decimal("3.75"))
        doc = PaymentReconciliation.objects.create(party_type="customer", customer=self.customer)
        doc.lines.create(payment=advance, sales_invoice=inv, amount=100)
        doc.submit()
        # الذمة: مدين 375 بالفاتورة، دائن 370 بالدفعة، والفرق 5 يُقفل خسارةَ صرف
        self.assertEqual(party_balance(customer=self.customer), 0)
        self.assertEqual(doc.exchange_difference, Decimal("-5"))
        self.assertEqual(party_balance(account=self.company.default_exchange_account), Decimal("5"))

    def test_pages_render(self):
        a = self.invoice(300)
        pay = self.payment(100, sales_invoice=a)
        self.client.force_login(self.user)
        for url in ("/app/payment-entry/new/", "/app/payment-reconciliation/new/", "/app/payment-reconciliation/",
                    pay.get_absolute_url(), a.get_absolute_url(), "/reports/accounts/reconcile/?party_type=supplier"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(a.get_absolute_url()), pay.name)
