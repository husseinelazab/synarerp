import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.credit import aging, check_credit
from accounts.models import PaymentEntry, PaymentTermsTemplate
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


class PaymentTermsTests(TestCase):
    """شروط الدفع بالأقساط: بناء الجدول، الاستحقاق المتحرك، وأعمار الديون لكل قسط."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الأقساط", "terms")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.item = Item.objects.create(code="T1", name="جهاز", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), is_stock_item=False, selling_rate=1000,
                                        vat_category="Z", vat_exemption_reason="VATEX-SA-32")
        self.terms = PaymentTermsTemplate.objects.create(name="30% مقدم و3 أقساط")
        self.terms.lines.create(description="دفعة مقدمة", invoice_portion=30, credit_days=0)
        for n in (1, 2, 3):
            self.terms.lines.create(description=f"القسط {n}", invoice_portion=Decimal("23.33") if n < 3 else Decimal("23.34"),
                                    credit_days=30 * n)
        self.customer = Customer.objects.create(name="عميل الأقساط", payment_terms=self.terms)

    def invoice(self, posting_date=None, **extra):
        inv = SalesInvoice.objects.create(customer=self.customer, tax_rate=0, vat_category="Z",
                                          vat_exemption_reason="VATEX-SA-32", update_stock=False,
                                          posting_date=posting_date or datetime.date.today(), **extra)
        inv.items.create(item=self.item, qty=1, rate=1000)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        inv.refresh_from_db()
        return inv

    def pay(self, inv, amount):
        pay = PaymentEntry.objects.create(payment_type="receive", customer=self.customer, amount=amount,
                                          account=self.company.default_cash_account, sales_invoice=inv)
        pay.submit()
        inv.refresh_from_db()

    def test_schedule_from_party_terms(self):
        inv = self.invoice()
        rows = list(inv.schedule.all())
        self.assertEqual([r.amount for r in rows], [Decimal("300"), Decimal("233.30"), Decimal("233.30"),
                                                   Decimal("233.40")])
        self.assertEqual(sum(r.amount for r in rows), inv.grand_total)
        self.assertEqual(inv.payment_terms, self.terms)
        self.assertEqual(inv.due_date, inv.posting_date)  # أول قسط (المقدم)

    def test_due_date_moves_with_payments(self):
        inv = self.invoice()
        self.pay(inv, 300)
        self.assertEqual(inv.due_date, inv.posting_date + datetime.timedelta(days=30))
        self.pay(inv, 400)  # يغطي القسط الأول وجزءاً من الثاني
        self.assertEqual(inv.due_date, inv.posting_date + datetime.timedelta(days=60))
        rows = inv.schedule_rows()
        self.assertEqual([r["outstanding"] for r in rows], [0, 0, Decimal("66.60"), Decimal("233.40")])

    def test_aging_buckets_per_installment(self):
        start = datetime.date.today() - datetime.timedelta(days=75)
        inv = self.invoice(posting_date=start)
        self.pay(inv, 300)
        row = aging("customer", party=self.customer)[0]
        # القسط الأول متأخر 45 يوماً، والثاني 15 يوماً، والثالث لم يستحق بعد
        self.assertEqual(row["buckets"], [Decimal("233.40"), Decimal("233.30"), Decimal("233.30"), 0, 0])
        self.assertEqual(row["oldest"], 45)

    def test_overdue_installment_blocks_credit(self):
        self.company.block_overdue_days = 10
        self.company.save()
        start = datetime.date.today() - datetime.timedelta(days=45)
        self.invoice(posting_date=start)  # المقدم متأخر 45 يوماً
        doc = SalesInvoice(customer=self.customer, company=self.company)
        with self.assertRaises(ValidationError):
            check_credit(doc, 100)

    def test_explicit_due_date_without_terms(self):
        plain = Customer.objects.create(name="عميل آجل")
        due = datetime.date.today() + datetime.timedelta(days=15)
        inv = SalesInvoice.objects.create(customer=plain, tax_rate=0, vat_category="Z",
                                          vat_exemption_reason="VATEX-SA-32", update_stock=False, due_date=due)
        inv.items.create(item=self.item, qty=1, rate=100)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        self.assertFalse(inv.schedule.exists())
        self.assertEqual(inv.due_date, due)

    def test_portions_must_total_100(self):
        bad = PaymentTermsTemplate.objects.create(name="ناقصة")
        bad.lines.create(invoice_portion=50, credit_days=0)
        with self.assertRaises(ValidationError):
            bad.schedule(datetime.date.today(), 100)

    def test_detail_page_shows_schedule(self):
        inv = self.invoice()
        self.client.force_login(self.user)
        page = self.client.get(inv.get_absolute_url())
        self.assertContains(page, "جدول الأقساط")
        self.assertContains(page, "دفعة مقدمة")
        self.assertEqual(self.client.get("/app/payment-terms/new/").status_code, 200)
