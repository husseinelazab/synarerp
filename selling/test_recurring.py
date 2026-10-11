import datetime
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from core.models import Notification
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, RecurringInvoice, SalesInvoice, add_period
from stock.models import UOM, Item, ItemGroup, Warehouse


class RecurringInvoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة العقود", "contracts")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        rent = Item.objects.create(code="RENT", name="إيجار محل", item_group=ItemGroup.objects.first(),
                                   uom=UOM.objects.first(), is_stock_item=False)
        self.customer = Customer.objects.create(name="مستأجر")
        self.template = SalesInvoice.objects.create(customer=self.customer, warehouse=Warehouse.objects.first(),
                                                    update_stock=False, discount_amount=100)
        self.template.items.create(item=rent, qty=1, rate=5000)
        self.template.calculate_totals()
        self.template.save()

    def test_add_period_month_end(self):
        self.assertEqual(add_period(datetime.date(2026, 1, 31), "monthly"), datetime.date(2026, 2, 28))
        self.assertEqual(add_period(datetime.date(2026, 1, 31), "monthly", 2), datetime.date(2026, 3, 31))
        self.assertEqual(add_period(datetime.date(2026, 11, 15), "quarterly"), datetime.date(2027, 2, 15))
        self.assertEqual(add_period(datetime.date(2026, 1, 1), "weekly", 2), datetime.date(2026, 1, 15))

    def test_catch_up_copies_and_submits(self):
        from core.models import FiscalYear
        start = (self.today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)   # أول الشهر الماضي
        if not FiscalYear.objects.filter(start_date__lte=start, end_date__gte=start).exists():
            FiscalYear.objects.create(name=str(start.year), start_date=datetime.date(start.year, 1, 1),
                                      end_date=datetime.date(start.year, 12, 31))
        recurring = RecurringInvoice.objects.create(name="إيجار المحل", template_invoice=self.template, frequency="monthly",
                                                    start_date=start, due_days=10)
        created = recurring.generate_due(self.today)
        self.assertEqual(len(created), 2)                                    # الشهر الماضي وهذا الشهر
        first = created[0]
        self.assertTrue(first.is_submitted)
        self.assertEqual((first.posting_date, first.due_date), (start, start + datetime.timedelta(days=10)))
        self.assertEqual((first.customer, first.discount_amount, first.grand_total),
                         (self.customer, Decimal("100"), Decimal("5635.00")))
        self.assertEqual(first.recurring, recurring)
        self.assertEqual(recurring.next_date, add_period(start, "monthly", 2))
        self.assertEqual(recurring.generate_due(self.today), [])             # لا تكرار
        self.assertEqual(list(recurring.invoices()), created[::-1])

    def test_end_date_disabled_and_failed_submit_kept_as_draft(self):
        recurring = RecurringInvoice.objects.create(name="عقد قصير", template_invoice=self.template, frequency="weekly",
                                                    start_date=self.today - datetime.timedelta(days=20),
                                                    end_date=self.today - datetime.timedelta(days=10))
        self.assertEqual(len(recurring.generate_due(self.today)), 2)          # قبل الانتهاء فقط
        self.assertTrue(recurring.is_finished)
        self.customer.credit_hold = True                                      # البيع الآجل موقوف: يفشل الاعتماد
        self.customer.save()
        held = RecurringInvoice.objects.create(name="عميل موقوف", template_invoice=self.template,
                                               start_date=self.today)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("generate_recurring", stdout=open("/dev/null", "w"))
        held.refresh_from_db()
        self.assertTrue(held.last_invoice.is_draft)
        self.assertTrue(held.last_error)
        self.assertTrue(Notification.objects.filter(title__contains="عميل موقوف").exists())
        off = RecurringInvoice.objects.create(name="موقوف", template_invoice=self.template, start_date=self.today,
                                              enabled=False)
        self.assertEqual(off.generate_due(self.today), [])

    def test_pages(self):
        self.client.force_login(self.user)
        self.template.submit()
        page = self.client.get(self.template.get_absolute_url())
        self.assertContains(page, "جعلها متكررة")
        response = self.client.post("/app/recurring-invoice/new/", {
            "name": "صيانة سنوية", "template_invoice": self.template.pk, "frequency": "yearly",
            "start_date": self.today.isoformat(), "due_days": 30, "auto_submit": "on", "enabled": "on"})
        recurring = RecurringInvoice.objects.get(name="صيانة سنوية")
        self.assertRedirects(response, recurring.get_absolute_url(), fetch_redirect_response=False)
        self.client.post(f"/app/recurring-invoice/{recurring.pk}/generate/")
        recurring.refresh_from_db()
        self.assertEqual(recurring.generated_count, 1)
        self.assertContains(self.client.get(recurring.get_absolute_url()), recurring.last_invoice.name)
