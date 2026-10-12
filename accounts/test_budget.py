import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Account, Budget, CostCenter, JournalEntry
from core.models import FiscalYear, Notification
from core.tenancy import tenant_context
from saas.tests import make_company


class BudgetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الموازنة", "budgeter")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.year = FiscalYear.objects.get()
        self.rent = Account.objects.get(number="5202")
        self.today = datetime.date.today()

    def expense(self, amount, account=None, center=None):
        je = JournalEntry.objects.create(posting_date=self.today)
        je.accounts.create(account=account or self.rent, debit=amount, cost_center=center)
        je.accounts.create(account=Account.objects.get(number="1101"), credit=amount)
        je.submit()
        return je

    def budget(self, amount, action="stop", account=None, center=None, period="annual"):
        b = Budget.objects.create(name=f"موازنة {amount}{action}", fiscal_year=self.year, action=action,
                                  cost_center=center, control_period=period)
        b.lines.create(account=account or self.rent, amount=amount)
        return b

    def test_stop_when_exceeded(self):
        self.budget(10000)
        self.expense(8000)
        with self.assertRaises(ValidationError):
            self.expense(3000)
        self.expense(2000)  # يصل للحد تماماً

    def test_warn_notifies(self):
        self.budget(1000, action="warn")
        self.expense(1500)
        self.assertTrue(Notification.objects.filter(title__contains="تجاوز الموازنة").exists())

    def test_group_account_and_cost_center_scope(self):
        group = Account.objects.get(number="52")
        branch = CostCenter.objects.create(name="فرع جدة", parent=CostCenter.objects.get(is_group=True))
        self.budget(500, account=group, center=branch)
        self.expense(5000)  # الفرع الرئيسي خارج نطاق الموازنة
        with self.assertRaises(ValidationError):
            self.expense(600, center=branch)

    def test_report(self):
        b = self.budget(12000, action="warn")
        self.expense(3000)
        self.client.force_login(self.user)
        page = self.client.get(f"/reports/accounts/budget/?budget={b.pk}")
        self.assertEqual(page.context["rows"][0]["actual"], Decimal("3000"))
        self.assertEqual(self.client.get("/app/budget/new/").status_code, 200)
