import datetime
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase

from accounts.models import ExchangeRevaluation, GLEntry, PaymentEntry
from core.models import Currency, ExchangeRate
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


def balance(until=None, **filters):
    qs = GLEntry.objects.filter(is_cancelled=False, **filters)
    if until:
        qs = qs.filter(posting_date__lte=until)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class RevaluationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة التصدير", "fx")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        today = datetime.date.today()
        self.start = today.replace(month=1, day=2)
        self.usd = Currency.objects.get(code="USD")
        ExchangeRate.objects.create(currency=self.usd, date=self.start, rate=Decimal("3.75"))
        item = Item.objects.create(code="FX", name="تصدير", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                   is_stock_item=False, vat_category="Z", vat_exemption_reason="VATEX-SA-32")
        self.customer = Customer.objects.create(name="عميل أجنبي")
        self.inv = SalesInvoice.objects.create(customer=self.customer, currency=self.usd, posting_date=self.start,
                                               tax_rate=0, vat_category="Z", vat_exemption_reason="VATEX-SA-32",
                                               update_stock=False)
        self.inv.items.create(item=item, qty=1, rate=1000)
        self.inv.calculate_totals()
        self.inv.save()
        self.inv.submit()

    def test_revalue_and_auto_reverse(self):
        day = self.start + datetime.timedelta(days=1)
        ExchangeRate.objects.create(currency=self.usd, date=day, rate=Decimal("3.80"))
        rev = ExchangeRevaluation.objects.create(posting_date=day)
        rev.calculate_totals()
        rev.save()
        self.assertEqual(rev.lines.get().difference, Decimal("50"))
        rev.submit()
        fx = self.company.default_exchange_account
        self.assertEqual(balance(account=fx, until=day), Decimal("-50"))  # ربح غير محقق
        self.assertEqual(balance(customer=self.customer, until=day), Decimal("3800"))
        self.assertEqual(balance(customer=self.customer), Decimal("3750"))  # بعد العكس
        # السداد لاحقاً بسعر 3.80 يحقق الربح الكامل مرة واحدة فقط
        pay = PaymentEntry.objects.create(payment_type="receive", customer=self.customer, amount=1000, currency=self.usd,
                                          exchange_rate=Decimal("3.80"), sales_invoice=self.inv,
                                          account=self.company.default_cash_account,
                                          posting_date=day + datetime.timedelta(days=1))
        pay.submit()
        self.assertEqual(balance(account=fx), Decimal("-50"))
        rev.cancel()
        self.assertEqual(balance(account=fx), Decimal("-50"))

    def test_page(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/app/exchange-revaluation/new/").status_code, 200)
