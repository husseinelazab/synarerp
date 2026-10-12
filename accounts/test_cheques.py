import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, AccountType, Cheque, GLEntry
from buying.models import Supplier
from core.models import Notification
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


def balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class ChequeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الشيكات", "cheques")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.customer = Customer.objects.create(name="عميل الشيكات")
        self.bank = Account.objects.get(number="1102")
        self.pdc = Account.objects.get(account_type=AccountType.CHEQUES_RECEIVABLE)
        item = Item.objects.create(code="CQ", name="خدمة", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                   is_stock_item=False, vat_category="Z", vat_exemption_reason="VATEX-SA-32")
        self.invoice = SalesInvoice.objects.create(customer=self.customer, tax_rate=0, vat_category="Z",
                                                   vat_exemption_reason="VATEX-SA-32", update_stock=False)
        self.invoice.items.create(item=item, qty=1, rate=5000)
        self.invoice.calculate_totals()
        self.invoice.save()
        self.invoice.submit()
        self.later = datetime.date.today() + datetime.timedelta(days=2)

    def received(self, **extra):
        cheque = Cheque.objects.create(direction="received", customer=self.customer, cheque_no="100200",
                                       drawer_bank="الراجحي", cheque_date=self.later, amount=5000,
                                       sales_invoice=self.invoice, **extra)
        cheque.submit()
        return cheque

    def test_received_cheque_lifecycle(self):
        cheque = self.received()
        self.invoice.refresh_from_db()
        self.assertEqual(cheque.status, "in_hand")
        self.assertEqual(self.invoice.outstanding_amount, 0)  # الفاتورة سُددت بالشيك
        self.assertEqual(balance(account=self.pdc), Decimal("5000"))
        self.assertEqual(balance(customer=self.customer), 0)
        cheque.mark_deposited()
        cheque.clear(self.user)
        self.assertEqual(cheque.status, "cleared")
        self.assertEqual(balance(account=self.pdc), 0)
        self.assertEqual(balance(account=self.bank), Decimal("5000"))
        # ارتداد بعد التحصيل يعيد الذمة على العميل والفاتورة غير مسددة
        cheque.bounce(self.user)
        self.invoice.refresh_from_db()
        self.assertEqual(cheque.status, "bounced")
        self.assertEqual(self.invoice.outstanding_amount, Decimal("5000"))
        self.assertEqual(balance(customer=self.customer), Decimal("5000"))
        self.assertEqual(balance(account=self.bank), 0)
        self.assertTrue(Notification.objects.filter(title__contains="ارتد الشيك").exists())

    def test_issued_cheque(self):
        supplier = Supplier.objects.create(name="مورد الشيكات")
        cheque = Cheque.objects.create(direction="issued", supplier=supplier, cheque_no="9001", amount=1200,
                                       cheque_date=self.later, bank_account=self.bank)
        cheque.submit()
        payable_pdc = Account.objects.get(account_type=AccountType.CHEQUES_PAYABLE)
        self.assertEqual(balance(account=payable_pdc), Decimal("-1200"))
        self.assertEqual(balance(supplier=supplier), Decimal("1200"))  # دفعة مقدمة للمورد
        cheque.clear()
        self.assertEqual(balance(account=payable_pdc), 0)
        self.assertEqual(balance(account=self.bank), Decimal("-1200"))

    def test_duplicate_and_cancel(self):
        cheque = self.received(remarks="")
        dup = Cheque.objects.create(direction="received", customer=self.customer, cheque_no="100200",
                                    drawer_bank="الراجحي", cheque_date=self.later, amount=10)
        with self.assertRaises(ValidationError):
            dup.submit()
        cheque.cancel()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.outstanding_amount, Decimal("5000"))
        self.assertEqual(Cheque.objects.get(pk=cheque.pk).status, "cancelled")

    def test_alerts_and_pages(self):
        cheque = self.received()
        call_command("accounts_alerts", stdout=open("/dev/null", "w"))
        call_command("accounts_alerts", stdout=open("/dev/null", "w"))  # مرة واحدة يومياً
        self.assertEqual(Notification.objects.filter(title__contains="شيك مستحق").count(), 1)
        self.client.force_login(self.user)
        page = self.client.get("/reports/accounts/cheques/")
        self.assertContains(page, "100200")
        self.assertContains(self.client.get(cheque.get_absolute_url()), "تحصيل الشيك")
        self.assertEqual(self.client.get("/app/cheque/new/?direction=issued").status_code, 200)

    def test_existing_company_gets_cheque_accounts(self):
        from accounts.models import ChequeDirection, cheque_account

        self.pdc.delete()
        account = cheque_account(self.company, ChequeDirection.RECEIVED)
        self.assertEqual(account.account_type, AccountType.CHEQUES_RECEIVABLE)
        self.assertEqual(account.parent.number, "11")
