from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase

from accounts.models import Account, CashCustody, GLEntry, custody_account
from core.tenancy import tenant_context
from hr.models import Employee
from saas.tests import make_company


def balance(account):
    agg = GLEntry.objects.filter(is_cancelled=False, account=account).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class CustodyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة العهد", "custody")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.employee = Employee.objects.create(employee_number="C1", full_name="سالم", gender="male",
                                                date_of_joining="2026-01-01", basic_salary=4000)
        self.cash = self.company.default_cash_account

    def test_custody_and_settlement(self):
        custody = CashCustody.objects.create(employee=self.employee, amount=2000, paid_from=self.cash,
                                             purpose="مشتريات نثرية")
        custody.submit()
        holding = custody_account(self.company)
        self.assertEqual(holding.number, "1109")
        self.assertEqual(balance(holding), Decimal("2000"))
        settlement = custody.make_settlement()
        settlement.lines.create(account=Account.objects.get(number="5205"), description="أدوات مكتبية",
                                bill_no="B-1", amount=1000, tax_amount=150)
        settlement.returned_amount = 850
        settlement.calculate_totals()
        settlement.save()
        settlement.submit()
        custody.refresh_from_db()
        self.assertEqual((custody.status, custody.settled_amount), ("settled", Decimal("2000")))
        self.assertEqual(balance(holding), 0)
        self.assertEqual(balance(self.company.default_input_tax_account), Decimal("150"))
        with self.assertRaises(ValidationError):
            custody.cancel()
        settlement.cancel()
        custody.refresh_from_db()
        self.assertEqual(custody.status, "open")

    def test_over_settlement_rejected(self):
        custody = CashCustody.objects.create(employee=self.employee, amount=500, paid_from=self.cash, purpose="سفر")
        custody.submit()
        settlement = custody.make_settlement()
        settlement.lines.create(account=Account.objects.get(number="5205"), amount=600)
        settlement.calculate_totals()
        settlement.save()
        with self.assertRaises(ValidationError):
            settlement.submit()

    def test_pages(self):
        self.client.force_login(self.user)
        for slug in ("cash-custody", "custody-settlement"):
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200)
