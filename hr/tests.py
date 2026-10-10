import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import GLEntry
from core.models import DocStatus
from core.tenancy import tenant_context
from hr.models import Employee, LeaveApplication, LeaveType, PayrollRun, PayrollSettings
from saas.tests import make_company

YEAR = datetime.date.today().year
JUNE_END = datetime.date(YEAR, 6, 30)


def d(month, day):
    return datetime.date(YEAR, month, day)


class HRTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الرواتب", "hrboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.saudi = Employee.objects.create(
            employee_number="E1", full_name="سعود", gender="male", is_saudi=True, date_of_joining=d(1, 1),
            basic_salary=8000, housing_allowance=2000, transport_allowance=800, iban="SA0380000000608010167519")
        self.expat = Employee.objects.create(
            employee_number="E2", full_name="رحيم", gender="male", date_of_joining=d(1, 1),
            basic_salary=4000, housing_allowance=1000, transport_allowance=500)
        self.annual = LeaveType.objects.get(name="إجازة سنوية")
        self.unpaid = LeaveType.objects.get(name="إجازة بدون راتب")

    def leave(self, employee, leave_type, start, end, submit=True):
        lv = LeaveApplication.objects.create(employee=employee, leave_type=leave_type, from_date=start, to_date=end)
        lv.calculate_totals()
        lv.save()
        if submit:
            lv.submit()
        return lv

    def payroll(self, month=6, submit=False, **kwargs):
        run = PayrollRun.objects.create(year=YEAR, month=month, posting_date=datetime.date(YEAR, month, 28), **kwargs)
        run.calculate_totals()
        run.save()
        if submit:
            run.submit()
        return run

    def line(self, run, employee):
        return run.lines.get(employee=employee)

    # ── الإعداد ──
    def test_provisioning_creates_leave_types_settings_and_accounts(self):
        self.assertEqual(LeaveType.objects.count(), 7)
        settings = PayrollSettings.for_company(self.company)
        self.assertEqual(settings.salary_expense_account.number, "5201")
        self.assertEqual(settings.gosi_expense_account.number, "5209")
        self.assertEqual(settings.salary_payable_account.number, "2103")
        self.assertEqual(settings.gosi_payable_account.number, "2105")
        self.assertEqual(PayrollSettings.objects.count(), 1)

    # ── الإجازات ──
    def test_leave_days_balance_and_overlap(self):
        lv = self.leave(self.saudi, self.annual, d(3, 1), d(3, 15))
        self.assertEqual(lv.total_days, 15)
        with self.assertRaisesMessage(ValidationError, "تتداخل"):
            self.leave(self.saudi, self.annual, d(3, 10), d(3, 12))
        with self.assertRaisesMessage(ValidationError, "لا يكفي"):
            self.leave(self.saudi, self.annual, d(4, 1), d(4, 7))  # 15 + 7 > 21
        self.leave(self.saudi, self.annual, d(4, 1), d(4, 6))  # يكمل 21 بالضبط
        # الإلغاء يعيد الرصيد
        lv.cancel()
        self.leave(self.saudi, self.annual, d(5, 1), d(5, 10))

    def test_leave_outside_employment_rejected(self):
        self.expat.relieving_date = d(2, 28)
        self.expat.save()
        with self.assertRaisesMessage(ValidationError, "خارج فترة عمل"):
            self.leave(self.expat, self.annual, d(2, 25), d(3, 3))

    def test_leave_needs_no_fiscal_year(self):
        from core.models import FiscalYear
        FiscalYear.objects.update(is_closed=True)
        self.leave(self.saudi, self.annual, d(2, 1), d(2, 3))

    # ── الرواتب ──
    def test_payroll_fetches_employees_and_calculates_gosi(self):
        run = self.payroll()
        self.assertEqual(run.lines.count(), 2)
        s = self.line(run, self.saudi)
        self.assertEqual((s.gross_pay, s.gosi_employee, s.gosi_employer, s.net_pay),
                         (Decimal("10800.00"), Decimal("975.00"), Decimal("1175.00"), Decimal("9825.00")))
        e = self.line(run, self.expat)
        self.assertEqual((e.gross_pay, e.gosi_employee, e.gosi_employer, e.net_pay),
                         (Decimal("5500.00"), Decimal("0.00"), Decimal("100.00"), Decimal("5500.00")))
        self.assertEqual(run.total_net, Decimal("15325.00"))
        self.assertEqual(run.total_gosi_employer, Decimal("1275.00"))

    def test_gosi_capped_and_rates_configurable(self):
        self.saudi.basic_salary = 50000
        self.saudi.save()
        settings = PayrollSettings.for_company(self.company)
        settings.gosi_employee_rate = Decimal("10")
        settings.save()
        s = self.line(self.payroll(), self.saudi)
        self.assertEqual(s.gosi_employee, Decimal("4500.00"))  # 10% × 45,000

    def test_unpaid_leave_and_manual_items(self):
        self.leave(self.expat, self.unpaid, d(6, 10), d(6, 12))
        run = self.payroll()
        e = self.line(run, self.expat)
        e.additions, e.other_deductions = Decimal("300"), Decimal("200")
        e.save()
        run.calculate_totals()
        run.save()
        e.refresh_from_db()
        self.assertEqual((e.unpaid_days, e.unpaid_deduction), (3, Decimal("550.00")))  # 5500 / 30 × 3
        self.assertEqual(e.gross_pay, Decimal("5800.00"))
        self.assertEqual(e.net_pay, Decimal("5050.00"))  # 5800 − 550 − 200

    def test_partial_month_is_prorated(self):
        joiner = Employee.objects.create(employee_number="E3", full_name="جديد", gender="female",
                                         date_of_joining=d(6, 16), basic_salary=3000, housing_allowance=600)
        line = self.line(self.payroll(), joiner)
        self.assertEqual((line.worked_days, line.basic, line.housing), (15, Decimal("1500.00"), Decimal("300.00")))
        self.saudi.relieving_date = d(5, 31)
        self.saudi.save()
        self.assertFalse(self.payroll(month=7).lines.filter(employee=self.saudi).exists())

    def test_submit_posts_balanced_gl(self):
        self.leave(self.expat, self.unpaid, d(6, 10), d(6, 12))
        run = self.payroll()
        e = self.line(run, self.expat)
        e.other_deductions = Decimal("200")
        e.save()
        run.submit()
        entries = {g.account.number: g.debit - g.credit for g in GLEntry.objects.filter(voucher_type="payroll-run",
                                                                                       voucher_id=run.pk)}
        # 10800 + 5500 − 550 غياب − 200 استقطاع (يخفض المصروف)
        self.assertEqual(entries["5201"], Decimal("15550.00"))
        self.assertEqual(entries["5209"], Decimal("1275.00"))
        self.assertEqual(entries["2105"], Decimal("-2250.00"))  # 975 + 1175 + 100
        self.assertEqual(entries["2103"], Decimal("-14575.00"))  # 9825 + 4750
        self.assertEqual(sum(entries.values()), 0)
        self.assertEqual(run.total_cost, Decimal("17025.00"))

    def test_same_month_cannot_be_paid_twice(self):
        self.payroll(submit=True)
        with self.assertRaisesMessage(ValidationError, "مسجل في المسير المعتمد"):
            self.payroll(submit=True)

    def test_payment_entries_and_cancel_rules(self):
        run = self.payroll(submit=True)
        self.assertEqual([a for a, _ in run.available_actions], ["make_salary_payment", "make_gosi_payment"])
        je = run.make_salary_payment(self.user)
        self.assertEqual(je.total_debit, run.total_net)
        je.submit()
        gosi = run.make_gosi_payment(self.user)
        self.assertEqual(gosi.total_debit, Decimal("2250.00"))
        gosi.submit()
        payable = sum(g.debit - g.credit for g in GLEntry.objects.filter(account__number__in=["2103", "2105"],
                                                                        is_cancelled=False))
        self.assertEqual(payable, 0)
        self.assertEqual(run.available_actions, [])
        with self.assertRaisesMessage(ValidationError, "ألغِ قيد الصرف"):
            run.cancel()
        je.cancel()
        gosi.cancel()
        run.cancel()
        self.assertEqual(run.docstatus, DocStatus.CANCELLED)

    def test_unpaid_leave_blocked_after_payroll(self):
        self.payroll(submit=True)
        with self.assertRaisesMessage(ValidationError, "صُرف في المسير"):
            self.leave(self.expat, self.unpaid, d(6, 20), d(6, 21))
        self.leave(self.expat, self.annual, d(6, 20), d(6, 21))  # المدفوعة لا تؤثر

    def test_payroll_requires_open_fiscal_year(self):
        from core.models import FiscalYear
        FiscalYear.objects.update(is_closed=True)
        with self.assertRaisesMessage(ValidationError, "مقفلة"):
            self.payroll(submit=True)

    def test_negative_net_blocked(self):
        run = self.payroll()
        e = self.line(run, self.expat)
        e.other_deductions = Decimal("9000")
        e.save()
        with self.assertRaisesMessage(ValidationError, "بالسالب"):
            run.submit()

    def test_iban_validation(self):
        self.expat.iban = "SA12"
        with self.assertRaises(ValidationError):
            self.expat.full_clean()

    # ── الواجهات ──
    def test_pages(self):
        self.leave(self.saudi, self.annual, d(3, 1), d(3, 5))
        response = self.client.post(reverse("doc_new", args=["payroll-run"]), {
            "year": YEAR, "month": 6, "posting_date": "%d-06-28" % YEAR, "remarks": "",
            "lines-TOTAL_FORMS": 0, "lines-INITIAL_FORMS": 0, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
        })
        run = PayrollRun.objects.get()
        self.assertRedirects(response, run.get_absolute_url())
        self.assertEqual(run.lines.count(), 2)
        self.assertEqual(run.total_net, Decimal("15325.00"))
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "طباعة قسائم الرواتب")
        self.assertContains(page, "صافي الراتب")
        self.assertContains(self.client.get(reverse("payslips", args=[run.pk])), "قسيمة راتب شهر")
        run.submit()
        response = self.client.get(reverse("payroll_bank_file", args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Missing-IBAN"], "1")
        self.assertContains(self.client.get(reverse("leave_balances")), "استُهلك 5 من 21")
        self.assertContains(self.client.get(reverse("payroll_settings")), "45")
        for slug in ("leave-application", "leave-type", "payroll-run"):
            self.assertEqual(self.client.get(reverse("doc_list", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_new", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_import_template", args=[slug, "xlsx"])).status_code, 200)

    def test_settings_page_manager_only(self):
        from core.models import Membership, Role
        from django.contrib.auth.models import User

        clerk = User.objects.create_user("clerk", password="x")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER)
        self.client.force_login(clerk)
        self.assertNotEqual(self.client.get(reverse("payroll_settings")).status_code, 200)
