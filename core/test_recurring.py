import datetime
from decimal import Decimal

from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, DeferralSchedule, GLEntry
from core.models import RecurringDocument
from core.recurring import make_recurring
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


class RecurringTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الإيجارات", "recurring")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.year_start = datetime.date.today().replace(month=1, day=1)
        item = Item.objects.create(code="RENT", name="إيجار محل", item_group=ItemGroup.objects.first(),
                                   uom=UOM.objects.first(), is_stock_item=False)
        self.inv = SalesInvoice.objects.create(customer=Customer.objects.create(name="مستأجر"), update_stock=False,
                                               posting_date=self.year_start, due_date=self.year_start + datetime.timedelta(days=10))
        self.inv.items.create(item=item, qty=1, rate=5000)
        self.inv.calculate_totals()
        self.inv.save()
        self.inv.submit()

    def test_monthly_invoices_generated(self):
        rec = make_recurring(self.inv, "monthly", submit_automatically=True)
        self.assertEqual(rec.next_date, self.year_start.replace(month=2))
        call_command("run_recurring", stdout=open("/dev/null", "w"))
        rec.refresh_from_db()
        today = datetime.date.today()
        expected = today.month - 1  # فبراير حتى الشهر الحالي
        self.assertEqual(rec.generated_count, expected)
        copies = SalesInvoice.objects.exclude(pk=self.inv.pk).order_by("posting_date")
        self.assertEqual(copies.count(), expected)
        if expected:
            first = copies.first()
            self.assertTrue(first.is_submitted)
            self.assertEqual((first.grand_total, first.due_date), (self.inv.grand_total,
                                                                 first.posting_date + datetime.timedelta(days=10)))
        self.assertGreater(rec.next_date, today)

    def test_draft_mode_and_page(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.inv.get_absolute_url()), "جعله متكرراً")
        response = self.client.post(f"/app/sales-invoice/{self.inv.pk}/recurring/",
                                    {"frequency": "monthly", "next_date": datetime.date.today().isoformat()})
        rec = RecurringDocument.objects.get()
        self.assertRedirects(response, f"/app/recurring-document/{rec.pk}/")
        doc = rec.generate()
        self.assertTrue(doc.is_draft)
        self.assertEqual(self.client.get(f"/app/recurring-document/{rec.pk}/").status_code, 200)

    def test_prepaid_rent_amortized(self):
        prepaid, rent = Account.objects.get(number="1106"), Account.objects.get(number="5202")
        schedule = DeferralSchedule.objects.create(kind="prepaid", balance_account=prepaid, pl_account=rent,
                                                   amount=12000, start_date=self.year_start, months=12)
        schedule.submit()
        self.assertEqual(schedule.entries.count(), 12)
        call_command("run_recurring", stdout=open("/dev/null", "w"))
        schedule.refresh_from_db()
        months = datetime.date.today().month
        self.assertEqual(schedule.posted_amount, Decimal(1000 * months))
        agg = GLEntry.objects.filter(account=rent, is_cancelled=False).aggregate(d=Sum("debit"))
        self.assertEqual(agg["d"], Decimal(1000 * months))
        schedule.cancel()
        self.assertFalse(GLEntry.objects.filter(account=rent, is_cancelled=False).exists())
