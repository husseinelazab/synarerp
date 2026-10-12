from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Account, JournalEntry
from buying.models import PurchaseInvoice, Supplier
from core.tenancy import tenant_context
from projects.models import Project
from saas.tests import make_company
from selling.models import Customer
from stock.models import UOM, Item, ItemGroup


class ProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة المقاولات", "projects")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.service = Item.objects.create(code="SRV", name="أعمال تنفيذ", item_group=ItemGroup.objects.first(),
                                           uom=UOM.objects.first(), is_stock_item=False)
        self.project = Project.objects.create(name="فيلا الملقا", customer=Customer.objects.create(name="مالك"),
                                              contract_value=100000, cost_budget=60000, billing_item=self.service)
        self.project.tasks.create(title="الحفر", weight=1, status="done")
        self.project.tasks.create(title="الهيكل", weight=3, progress=50)

    def test_progress_billing_and_profitability(self):
        self.assertEqual(self.project.progress, Decimal("62.50"))  # (100×1 + 50×3) / 4
        invoice = self.project.make_progress_invoice()
        self.assertEqual(invoice.items.get().rate, Decimal("62500"))
        invoice.submit()
        with self.assertRaises(ValidationError):  # لا مستحق جديد قبل تقدم الإنجاز
            self.project.make_progress_invoice()
        bill = PurchaseInvoice.objects.create(supplier=Supplier.objects.create(name="مقاول باطن"), project=self.project,
                                              update_stock=False)
        bill.items.create(item=self.service, qty=1, rate=30000)
        bill.calculate_totals(); bill.save(); bill.submit()
        je = JournalEntry.objects.create()
        je.accounts.create(account=Account.objects.get(number="5201"), debit=5000, project=self.project)
        je.accounts.create(account=Account.objects.get(number="1101"), credit=5000)
        je.submit()
        totals = self.project.gl_totals()
        self.assertEqual((totals["revenue"], totals["cost"], totals["profit"]),
                         (Decimal("62500"), Decimal("35000"), Decimal("27500")))
        self.client.force_login(self.user)
        page = self.client.get("/projects/report/")
        row = page.context["rows"][0]
        self.assertEqual((row["billed"], row["to_bill"]), (Decimal("62500"), 0))
        self.assertContains(self.client.get(self.project.get_absolute_url()), "الهيكل")

    def test_return_keeps_project(self):
        invoice = self.project.make_progress_invoice()
        invoice.submit()
        ret = invoice.make_return()
        self.assertEqual(ret.project, self.project)
