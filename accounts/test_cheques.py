import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.cheques import bounce_cheque, cheques_account, clear_cheque, undo_cheque
from accounts.models import Account, Cheque, GLEntry, PaymentEntry
from buying.models import Supplier
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse


def balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class ChequeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الشيكات", "chequer")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        self.bank = Account.objects.get(number="1102")
        self.customer = Customer.objects.create(name="عميل الشيكات")
        self.supplier = Supplier.objects.create(name="مورد الشيكات")
        item = Item.objects.create(code="CHQ", name="خدمة", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                   is_stock_item=False)
        self.invoice = SalesInvoice.objects.create(customer=self.customer, warehouse=Warehouse.objects.first(),
                                                   update_stock=False)
        self.invoice.items.create(item=item, qty=1, rate=1000)
        self.invoice.submit()                                               # 1150 شامل الضريبة

    def receive(self, amount=1150, **kwargs):
        payment = PaymentEntry.objects.create(
            payment_type="receive", customer=self.customer, sales_invoice=self.invoice, account=self.bank, amount=amount,
            payment_mode="cheque", cheque_no="000123", cheque_date=self.today + datetime.timedelta(days=30),
            drawee_bank="بنك الرياض", **kwargs)
        payment.submit()
        return payment

    def test_received_cheque_held_then_cleared(self):
        payment = self.receive()
        holding = self.company.default_cheques_receivable_account
        self.assertEqual(holding.number, "1108")
        self.assertEqual(balance(account=holding), Decimal("1150"))
        self.assertEqual(balance(account=self.bank), 0)                    # لم يصل البنك بعد
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.outstanding_amount, 0)
        cheque = payment.cheque
        self.assertEqual(cheque.status, "pending")
        with self.assertRaises(ValidationError):                          # الصرف قبل تاريخ السند
            clear_cheque(cheque, self.today - datetime.timedelta(days=1))
        clear_cheque(cheque, self.today)
        self.assertEqual(cheque.status, "cleared")
        self.assertEqual(balance(account=holding), 0)
        self.assertEqual(balance(account=self.bank), Decimal("1150"))
        with self.assertRaises(ValidationError):                          # لا يُلغى سند شيك مصروف
            payment.cancel()
        undo_cheque(cheque)
        cheque.refresh_from_db()
        self.assertEqual(cheque.status, "pending")
        self.assertEqual(balance(account=self.bank), 0)
        payment.cancel()
        cheque.refresh_from_db()
        self.assertEqual(cheque.status, "cancelled")
        self.assertEqual(balance(), 0)

    def test_bounced_cheque_reopens_invoice(self):
        payment = self.receive()
        bounce_cheque(payment.cheque, self.today, "رصيد غير كافٍ")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.outstanding_amount, Decimal("1150"))
        self.assertEqual(balance(customer=self.customer), Decimal("1150"))
        self.assertEqual(balance(account=self.company.default_cheques_receivable_account), 0)
        payment.cheque.bounce_entry.cancel()                               # إلغاء قيد الارتداد من صفحته
        cheque = Cheque.objects.get()
        self.invoice.refresh_from_db()
        self.assertEqual((cheque.status, self.invoice.outstanding_amount), ("pending", 0))

    def test_issued_cheque(self):
        payment = PaymentEntry.objects.create(payment_type="pay", supplier=self.supplier, account=self.bank, amount=500,
                                              payment_mode="cheque", cheque_no="900", cheque_date=self.today)
        payment.submit()
        payable = self.company.default_cheques_payable_account
        self.assertEqual(balance(account=payable), Decimal("-500"))
        self.assertEqual(balance(supplier=self.supplier), Decimal("500"))
        clear_cheque(payment.cheque, self.today)
        self.assertEqual((balance(account=payable), balance(account=self.bank)), (0, Decimal("-500")))

    def test_validation_and_existing_company_account(self):
        missing = PaymentEntry.objects.create(payment_type="receive", customer=self.customer, account=self.bank,
                                              amount=10, payment_mode="cheque")
        with self.assertRaisesMessage(ValidationError, "رقم الشيك"):
            missing.submit()
        # شركة قديمة بلا حساب شيكات: يُنشأ تلقائياً بجانب حساب العملاء
        self.company.default_cheques_receivable_account = None
        self.company.save()
        Account.objects.filter(number="1108").update(number="1108-old", name="حساب قديم")
        account = cheques_account(self.company, True)
        self.assertEqual((account.name, account.parent, account.root_type),
                         ("شيكات تحت التحصيل", self.company.default_receivable_account.parent, "asset"))

    def test_report_and_actions_over_http(self):
        payment = self.receive()
        self.client.force_login(self.user)
        page = self.client.get("/reports/accounts/cheques/")
        self.assertContains(page, "000123")
        self.assertContains(self.client.get(payment.get_absolute_url()), "تم التحصيل")
        self.client.post(f"/reports/accounts/cheques/{payment.cheque.pk}/action/",
                         {"action": "clear", "date": self.today.isoformat(), "next": "https://evil.example/"})
        self.assertEqual(Cheque.objects.get().status, "cleared")
        self.assertEqual(self.client.get("/app/payment-entry/new/").status_code, 200)
