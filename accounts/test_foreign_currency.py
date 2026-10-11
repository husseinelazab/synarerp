import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import (Account, BankTransaction, ExchangeRevaluation, GLEntry, JournalEntry, OpeningAccountBalance,
                             PaymentEntry)
from buying.models import PurchaseInvoice, Supplier
from core.models import Currency, ExchangeRate
from core.tenancy import tenant_context
from saas.tests import make_company
from stock.models import UOM, Item, ItemGroup, Warehouse


def balances(account):
    agg = GLEntry.objects.filter(account=account, is_cancelled=False).aggregate(
        d=Sum("debit"), c=Sum("credit"), fd=Sum("debit_in_account_currency"), fc=Sum("credit_in_account_currency"))
    return (agg["fd"] or 0) - (agg["fc"] or 0), (agg["d"] or 0) - (agg["c"] or 0)


def total_balance():
    agg = GLEntry.objects.filter(is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class ForeignCurrencyAccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الحسابات الأجنبية", "fxbank")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        self.usd = Currency.objects.get(code="USD")
        self.usd_bank = Account.objects.create(name="بنك الراجحي - دولار", number="1109", parent=Account.objects.get(number="1102").parent,
                                               root_type="asset", account_type="bank", account_currency=self.usd)
        self.usd_bank.full_clean()
        self.fx = self.company.default_exchange_account
        self.supplier = Supplier.objects.create(name="Global Supplies Inc")

    def opening(self, usd, sar):
        doc = OpeningAccountBalance.objects.create(posting_date=self.today - datetime.timedelta(days=10))
        doc.lines.create(account=self.usd_bank, debit=sar, account_currency_amount=usd)
        doc.submit()
        return doc

    def test_account_currency_rules(self):
        cash = Account.objects.get(number="1101")
        cash.account_currency = self.company.currency
        cash.full_clean()                                  # عملة الشركة تُحفظ فارغة
        self.assertIsNone(cash.account_currency)
        income = Account.objects.get(number="4104")
        income.account_currency = self.usd
        with self.assertRaises(ValidationError):
            income.full_clean()
        self.opening(1000, 3750)
        self.usd_bank.account_currency = Currency.objects.get(code="EUR")
        with self.assertRaises(ValidationError):           # لا تغيير لعملة حساب عليه قيود
            self.usd_bank.full_clean()

    def test_opening_requires_amount_in_account_currency(self):
        doc = OpeningAccountBalance.objects.create(posting_date=self.today)
        doc.lines.create(account=self.usd_bank, debit=3750)
        with self.assertRaises(ValidationError):
            doc.submit()
        self.opening(1000, 3750)
        self.assertEqual(balances(self.usd_bank), (Decimal("1000"), Decimal("3750")))

    def test_payment_tracks_dollars_and_rejects_other_currency(self):
        self.opening(1000, 3750)
        item = Item.objects.create(code="IMP", name="جهاز", item_group=ItemGroup.objects.first(), uom=UOM.objects.first())
        inv = PurchaseInvoice.objects.create(supplier=self.supplier, warehouse=Warehouse.objects.first(), currency=self.usd,
                                             exchange_rate=Decimal("3.75"))
        inv.items.create(item=item, qty=2, rate=100)
        inv.submit()
        pay = PaymentEntry.objects.create(payment_type="pay", supplier=self.supplier, purchase_invoice=inv,
                                          account=self.usd_bank, amount=230, currency=self.usd, exchange_rate=Decimal("3.76"))
        pay.submit()
        self.assertEqual(balances(self.usd_bank), (Decimal("770"), Decimal("2885.20")))
        self.assertEqual(total_balance(), 0)
        sar = PaymentEntry.objects.create(payment_type="pay", supplier=self.supplier, account=self.usd_bank, amount=100)
        with self.assertRaises(ValidationError):           # سند بالريال على حساب بالدولار
            sar.submit()
        pay.cancel()
        self.assertEqual(balances(self.usd_bank), (Decimal("1000"), Decimal("3750")))

    def test_journal_entry_needs_account_currency_amount(self):
        je = JournalEntry.objects.create(posting_date=self.today)
        je.accounts.create(account=self.usd_bank, debit=375)
        je.accounts.create(account=Account.objects.get(number="1101"), credit=375)
        je.calculate_totals()
        with self.assertRaises(ValidationError):
            je.submit()
        JournalEntry.objects.filter(pk=je.pk).update(docstatus=0)
        je.refresh_from_db()
        je.accounts.filter(account=self.usd_bank).update(account_currency_amount=100)
        je.submit()
        self.assertEqual(balances(self.usd_bank), (Decimal("100"), Decimal("375")))

    def test_other_documents_cannot_post_to_foreign_account(self):
        from accounts.ledger import make_gl_entries
        doc = JournalEntry.objects.create(posting_date=self.today)
        with self.assertRaises(ValidationError):
            make_gl_entries(doc, [{"account": self.usd_bank, "debit": 10},
                                  {"account": Account.objects.get(number="1101"), "credit": 10}])

    def test_revaluation_books_unrealized_gain_and_reverses_on_cancel(self):
        self.opening(1000, 3750)
        ExchangeRate.objects.create(currency=self.usd, date=self.today, rate="3.80")
        doc = ExchangeRevaluation.objects.create(posting_date=self.today)
        doc.calculate_totals()                              # الجدول الفارغ يُعبأ تلقائياً
        doc.save()
        line = doc.lines.get()
        self.assertEqual(line.account, self.usd_bank)
        self.assertEqual(line.new_rate, Decimal("3.80"))
        self.assertEqual(line.difference, Decimal("50.00"))
        doc.submit()
        self.assertEqual(doc.total_difference, Decimal("50.00"))
        self.assertEqual(balances(self.usd_bank), (Decimal("1000"), Decimal("3800")))
        fx = GLEntry.objects.filter(account=self.fx, is_cancelled=False).aggregate(c=Sum("credit"))["c"]
        self.assertEqual(fx, Decimal("50.00"))
        self.assertEqual(total_balance(), 0)
        again = ExchangeRevaluation.objects.create(posting_date=self.today)
        again.calculate_totals()
        with self.assertRaises(ValidationError):           # مقيّم بالفعل بنفس السعر
            again.submit()
        doc.cancel()
        self.assertEqual(balances(self.usd_bank), (Decimal("1000"), Decimal("3750")))

    def test_revaluation_with_manual_rate_and_loss(self):
        self.opening(1000, 3750)
        doc = ExchangeRevaluation.objects.create(posting_date=self.today)
        doc.lines.create(account=self.usd_bank)
        doc.calculate_totals()
        with self.assertRaises(ValidationError):           # لا يوجد سعر صرف
            doc.submit()
        doc.lines.update(new_rate=Decimal("3.70"))
        doc.submit()
        self.assertEqual(balances(self.usd_bank), (Decimal("1000"), Decimal("3700")))
        self.assertEqual(doc.total_difference, Decimal("-50.00"))

    def test_bank_reconciliation_matches_in_account_currency(self):
        self.opening(1000, 3750)
        ExchangeRate.objects.create(currency=self.usd, date=self.today, rate="3.80")
        reval = ExchangeRevaluation.objects.create(posting_date=self.today)
        reval.calculate_totals()
        reval.submit()
        BankTransaction.objects.create(account=self.usd_bank, date=self.today - datetime.timedelta(days=10), deposit=1000)
        from accounts.bank import auto_match, reconciliation_summary
        self.assertEqual(auto_match(self.usd_bank, self.today), 1)
        summary = reconciliation_summary(self.usd_bank, self.today, Decimal("1000"))
        self.assertEqual(summary["book_balance"], Decimal("1000"))
        self.assertEqual(summary["difference"], 0)

    def test_reports_and_pages(self):
        self.opening(1000, 3750)
        ExchangeRate.objects.create(currency=self.usd, date=self.today, rate="3.80")
        self.client.force_login(self.user)
        report = self.client.get("/reports/accounts/foreign-balances/")
        self.assertContains(report, "بنك الراجحي - دولار")
        self.assertEqual(report.context["total_difference"], Decimal("50.00"))
        ledger = self.client.get("/reports/accounts/general-ledger/", {"account": self.usd_bank.pk})
        self.assertEqual(ledger.context["foreign_closing"], Decimal("1000"))
        self.assertContains(ledger, "الرصيد USD")
        self.assertEqual(self.client.get("/app/exchange-revaluation/new/").status_code, 200)
        self.client.post("/app/exchange-revaluation/new/", {
            "posting_date": self.today.isoformat(), "remarks": "",
            "lines-TOTAL_FORMS": 0, "lines-INITIAL_FORMS": 0, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
        })
        reval = ExchangeRevaluation.objects.get()
        self.assertEqual(reval.total_difference, Decimal("50.00"))      # الحسابات جُلبت عند الحفظ
        reval.submit()
        self.assertContains(self.client.get(f"/app/exchange-revaluation/{reval.pk}/"), "القيمة بعد التقييم")
        self.assertEqual(self.client.get(f"/app/account/{self.usd_bank.pk}/edit/").status_code, 200)
        self.assertContains(self.client.get("/reports/accounts/bank-reconciliation/", {"account": self.usd_bank.pk}),
                            "بيان التسوية")
