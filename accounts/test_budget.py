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
        cls.company, cls.user = make_company("شركة الموازنات", "budgeter")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.today = datetime.date.today()
        self.year = FiscalYear.objects.get(start_date__lte=self.today, end_date__gte=self.today)
        self.rent, self.bank = Account.objects.get(number="5202"), Account.objects.get(number="1102")
        self.branch = CostCenter.objects.create(name="فرع جدة")

    def budget(self, amount, action="stop", center=None):
        budget = Budget.objects.create(fiscal_year=self.year, action=action, cost_center=center)
        budget.lines.create(account=self.rent, amount=amount)
        budget.submit()
        return budget

    def spend(self, amount, center=None):
        je = JournalEntry.objects.create(posting_date=self.today)
        je.accounts.create(account=self.rent, debit=amount, cost_center=center)
        je.accounts.create(account=self.bank, credit=amount)
        je.calculate_totals()
        je.submit()
        return je

    def test_stop_blocks_overspending(self):
        budget = self.budget(1000)
        self.assertEqual(budget.total_amount, Decimal("1000"))
        self.spend(600)
        with self.assertRaisesMessage(ValidationError, "تجاوز موازنة"):
            self.spend(500)
        self.assertEqual(JournalEntry.objects.filter(docstatus=1).count(), 1)
        self.spend(400)                                                    # يصل للحد تماماً
        with self.assertRaises(ValidationError):                           # موازنة ثانية لنفس السنة والمركز
            self.budget(5000)

    def test_warn_notifies_managers_and_posts(self):
        self.budget(100, action="warn")
        with self.captureOnCommitCallbacks(execute=True):
            je = self.spend(150)
        self.assertTrue(je.is_submitted)
        self.assertTrue(Notification.objects.filter(user=self.user, title__contains="تجاوز موازنة").exists())

    def test_cost_center_budget_counts_only_its_branch(self):
        self.budget(1000, center=self.branch)
        self.spend(5000)                                                   # المركز الافتراضي لا يخص الفرع
        self.spend(900, center=self.branch)
        with self.assertRaises(ValidationError):
            self.spend(200, center=self.branch)

    def test_validation_and_report(self):
        bad = Budget.objects.create(fiscal_year=self.year)
        bad.lines.create(account=self.bank, amount=10)
        with self.assertRaisesMessage(ValidationError, "المصروفات"):
            bad.submit()
        budget = self.budget(12000, action="none")
        self.spend(15000)                                                  # المتابعة فقط لا تمنع
        self.client.force_login(self.user)
        response = self.client.get("/reports/accounts/budget-variance/", {"budget": budget.pk})
        row = response.context["rows"][0]
        self.assertEqual(row["actual"], Decimal("15000"))
        self.assertEqual(row["remaining"], Decimal("-3000"))
        self.assertEqual(row["used"], Decimal("125.0"))
        self.assertContains(response, "الموازنة مقابل الفعلي")
        self.assertEqual(self.client.get("/app/budget/new/").status_code, 200)
