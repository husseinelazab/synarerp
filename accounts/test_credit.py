import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.credit import aging, party_balance
from accounts.models import PaymentEntry
from core.models import Company, Membership, Role
from selling.models import Customer, SalesInvoice, SalesOrder
from stock.models import Item

TODAY = timezone.localdate()


class CreditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.item = Item.objects.get(code="SRV-001")  # خدمة 150 بلا مخزون
        cls.customer = Customer.objects.create(company=cls.company, name="عميل آجل", phone="0500000009")

    def setUp(self):
        self.client.force_login(self.owner)

    def invoice(self, amount, due_offset=30, submit=True, posting_offset=0, customer=None):
        inv = SalesInvoice.objects.create(company=self.company, customer=customer or self.customer, update_stock=False,
                                          due_date=TODAY + datetime.timedelta(days=max(due_offset, 0)))
        inv.items.create(item=self.item, qty=1, rate=amount)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
            if due_offset < 0:  # فاتورة قديمة متأخرة
                SalesInvoice.objects.filter(pk=inv.pk).update(due_date=TODAY + datetime.timedelta(days=due_offset))
                inv.refresh_from_db()
        return inv

    def test_aging_buckets_and_unallocated(self):
        self.invoice(100, due_offset=5)                       # غير مستحقة
        self.invoice(200, due_offset=-10)                     # 1-30
        self.invoice(300, due_offset=-45)                     # 31-60
        self.invoice(400, due_offset=-120)                    # +90
        pe = PaymentEntry.objects.create(company=self.company, payment_type="receive", customer=self.customer,
                                         account=self.company.default_cash_account, amount=50)  # دفعة غير مربوطة
        pe.submit()
        row = next(r for r in aging("customer") if r["party"] == self.customer)
        totals = [b for b in row["buckets"]]
        self.assertEqual(totals[0], 115)
        self.assertEqual(totals[1], 230)
        self.assertEqual(totals[2], 345)
        self.assertEqual(totals[4], 460)
        self.assertEqual(row["overdue"], 1035)
        self.assertEqual(row["unallocated"], Decimal("-50"))
        self.assertEqual(row["balance"], party_balance("customer", self.customer))
        self.assertEqual(row["oldest"], 120)
        page = self.client.get("/reports/accounts/aging/customer/", {"only": "overdue"})
        self.assertContains(page, "عميل آجل")
        self.assertContains(page, "أقدم تأخير 120")
        self.assertEqual(self.client.get("/reports/accounts/aging/supplier/").status_code, 200)

    def test_credit_limit_blocks_invoice_and_order(self):
        Customer.objects.filter(pk=self.customer.pk).update(credit_limit=1000)
        self.customer.refresh_from_db()
        first = self.invoice(500)  # 575 مع الضريبة
        second = self.invoice(400, submit=False)  # يضيف 460 → 1035 > 1000
        with self.assertRaises(ValidationError) as ctx:
            second.submit()
        self.assertIn("تجاوز حد الائتمان", str(ctx.exception))
        self.assertIn("المتاح 425.00", str(ctx.exception))
        order = SalesOrder.objects.create(company=self.company, customer=self.customer)
        order.items.create(item=self.item, qty=4, rate=150)
        order.calculate_totals()
        order.save()
        with self.assertRaises(ValidationError):
            order.submit()
        # بعد التحصيل يُسمح
        pe = PaymentEntry.objects.create(company=self.company, payment_type="receive", customer=self.customer,
                                         account=self.company.default_cash_account, amount=575, sales_invoice=first)
        pe.submit()
        second.submit()
        self.assertTrue(second.is_submitted)

    def test_hold_and_overdue_block_but_cash_allowed(self):
        Customer.objects.filter(pk=self.customer.pk).update(credit_hold=True)
        self.customer.refresh_from_db()
        with self.assertRaises(ValidationError) as ctx:
            self.invoice(100)
        self.assertIn("موقوف", str(ctx.exception))
        Customer.objects.filter(pk=self.customer.pk).update(credit_hold=False)
        self.customer.refresh_from_db()
        self.invoice(100, due_offset=-40)
        Company.objects.filter(pk=self.company.pk).update(block_overdue_days=30)
        self.company.refresh_from_db()
        self.customer.company = self.company
        with self.assertRaises(ValidationError) as ctx:
            self.invoice(50)
        self.assertIn("متأخرة أكثر من 30", str(ctx.exception))
        # المرتجع لا يُمنع
        original = SalesInvoice.objects.filter(customer=self.customer, docstatus=1).first()
        ret = original.make_return(self.owner)
        ret.submit()

    def test_pos_paid_sale_not_counted(self):
        from pos.models import POSProfile, POSShift
        from pos.services import checkout

        Customer.objects.filter(pk=self.customer.pk).update(credit_limit=10, credit_hold=True)
        cashier = User.objects.create_user("cashier", password="pass12345")
        Membership.objects.create(user=cashier, company=self.company, role=Role.CASHIER)
        shift = POSShift.open(POSProfile.objects.get(), cashier, opening_cash=0)
        inv = checkout(shift, cashier, [{"item": self.item.pk, "qty": 1}], customer_id=self.customer.pk, paid_cash=500)
        self.assertTrue(inv.is_submitted)

    def test_customer_card_shows_credit_panel(self):
        Customer.objects.filter(pk=self.customer.pk).update(credit_limit=1000)
        self.invoice(100, due_offset=-5)
        page = self.client.get(f"/app/customer/{self.customer.pk}/")
        self.assertContains(page, "الوضع الائتماني")
        self.assertContains(page, "المتاح للبيع الآجل")
        self.assertContains(page, "واتساب")
