import datetime
import io
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
        # + مخصص نهاية الخدمة لنصف سنة: 10800 × ¼ + 5500 × ¼
        self.assertEqual(run.total_eos_accrual, Decimal("4075.00"))
        self.assertEqual(entries["5210"], Decimal("4075.00"))
        self.assertEqual(run.total_cost, Decimal("21100.00"))

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


class EndOfServiceCalcTests(TestCase):
    def test_award_rules(self):
        from hr.eos import calculate, service_years

        start = datetime.date(2015, 1, 1)
        self.assertEqual(service_years(start, datetime.date(2024, 12, 31)), 10)
        # 5 سنوات × نصف شهر + 5 × شهر = 7.5 أشهر
        self.assertEqual(calculate(start, datetime.date(2024, 12, 31), 10000).award, Decimal("75000.00"))
        three = datetime.date(2017, 12, 31)
        self.assertEqual(calculate(start, three, 9000).award, Decimal("13500.00"))
        self.assertEqual(calculate(start, three, 9000, "resignation").award, Decimal("4500.00"))  # الثلث
        self.assertEqual(calculate(start, datetime.date(2015, 12, 31), 9000, "resignation").award, 0)  # أقل من سنتين
        # 7 سنوات: (5 × 3000 + 2 × 6000) × ⅔
        self.assertEqual(calculate(start, datetime.date(2021, 12, 31), 6000, "resignation").award, Decimal("18000.00"))
        self.assertEqual(calculate(start, three, 9000, "article_80").award, 0)
        self.assertEqual(calculate(start, three, 9000, "article_87").award, Decimal("13500.00"))


class EOSAndAttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الحضور", "attboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        # راتب 9000 (أساسي 6000) ليسهل الحساب: أجر اليوم 300، الساعة 37.5
        self.emp = Employee.objects.create(
            employee_number="A1", full_name="عامل", gender="male", date_of_joining=d(1, 1),
            basic_salary=6000, housing_allowance=2000, transport_allowance=1000, gosi_subscribed=False)
        self.settings = PayrollSettings.for_company(self.company)

    def payroll(self, month=6, submit=False):
        run = PayrollRun.objects.create(year=YEAR, month=month, posting_date=datetime.date(YEAR, month, 28))
        run.calculate_totals()
        run.save()
        if submit:
            run.submit()
        return run

    def att(self, day, status="present", check_in=None, check_out=None, overtime=0):
        from hr.models import Attendance
        return Attendance.objects.create(employee=self.emp, attendance_date=day, status=status,
                                         check_in=check_in, check_out=check_out, overtime_hours=overtime)

    # ── الحضور ──
    def test_attendance_late_and_validation(self):
        t = datetime.time
        a = self.att(d(6, 1), check_in=t(8, 40), check_out=t(17, 0))
        self.assertEqual((a.late_minutes, a.worked_hours), (40, Decimal("8.33")))
        self.assertEqual(self.att(d(6, 2), check_in=t(8, 10), check_out=t(16, 30)).late_minutes, 0)  # ضمن السماح
        with self.assertRaisesMessage(ValidationError, "بعد وقت الحضور"):
            self.att(d(6, 3), check_in=t(9, 0), check_out=t(8, 0))
        annual = LeaveType.objects.get(name="إجازة سنوية")
        lv = LeaveApplication.objects.create(employee=self.emp, leave_type=annual, from_date=d(6, 20), to_date=d(6, 21))
        lv.submit()
        with self.assertRaisesMessage(ValidationError, "إجازة معتمدة"):
            self.att(d(6, 20))
        with self.assertRaisesMessage(ValidationError, "على رأس العمل"):
            self.att(datetime.date(YEAR - 1, 12, 31))

    def test_attendance_flows_into_payroll(self):
        t = datetime.time
        self.settings.deduct_late = True
        self.settings.save()
        self.att(d(6, 1), "absent")
        self.att(d(6, 2), "half_day", check_in=t(8, 0), check_out=t(12, 0))
        self.att(d(6, 3), check_in=t(9, 0), check_out=t(17, 0))  # تأخير 60 دقيقة
        self.att(d(6, 4), check_in=t(8, 0), check_out=t(19, 0), overtime=2)
        line = self.payroll().lines.get()
        self.assertEqual((line.absent_days, line.absence_deduction), (Decimal("1.5"), Decimal("450.00")))
        self.assertEqual((line.late_minutes, line.late_deduction), (60, Decimal("37.50")))
        # (37.5 + 50% × 25) × 2 ساعة = 100
        self.assertEqual((line.overtime_hours, line.overtime_pay), (Decimal("2.00"), Decimal("100.00")))
        self.assertEqual(line.gross_pay, Decimal("9100.00"))
        self.assertEqual(line.net_pay, Decimal("8612.50"))

    def test_attendance_locked_after_payroll(self):
        record = self.att(d(6, 5), "absent")
        self.payroll(submit=True)
        record.status = "present"
        with self.assertRaisesMessage(ValidationError, "ألغِ المسير"):
            record.save()
        with self.assertRaisesMessage(ValidationError, "ألغِ المسير"):
            record.delete()
        with self.assertRaisesMessage(ValidationError, "ألغِ المسير"):
            self.att(d(6, 6), "absent")
        response = self.client.post(reverse("doc_action", args=["attendance", record.pk, "delete"]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(type(record).objects.filter(pk=record.pk).exists())

    def test_daily_attendance_page(self):
        url = reverse("attendance_daily") + f"?date={YEAR}-06-08"
        self.assertContains(self.client.get(url), self.emp.full_name)
        e = self.emp.id
        self.client.post(url, {"date": f"{YEAR}-06-08", f"status_{e}": "present", f"in_{e}": "08:30",
                               f"out_{e}": "17:00", f"ot_{e}": ""})
        from hr.models import Attendance
        record = Attendance.objects.get()
        self.assertEqual(record.late_minutes, 30)
        response = self.client.post(url, {"date": f"{YEAR}-06-08", f"status_{e}": "present", f"in_{e}": "18:00",
                                          f"out_{e}": "17:00"})
        self.assertContains(response, "بعد وقت الحضور")
        self.client.post(url, {"date": f"{YEAR}-06-08", f"status_{e}": ""})
        self.assertFalse(Attendance.objects.exists())
        monthly = self.client.get(reverse("attendance_monthly") + f"?year={YEAR}&month=6")
        self.assertContains(monthly, self.emp.full_name)

    # ── مخصص ومخالصة نهاية الخدمة ──
    def gl(self, doc):
        return {g.account.number: g.debit - g.credit for g in GLEntry.objects.filter(
            voucher_type=doc.doctype_slug, voucher_id=doc.pk)}

    def test_monthly_provision_accrues_and_trues_up(self):
        june = self.payroll(submit=True)
        # 6 أشهر = نصف سنة × نصف شهر × 9000
        self.assertEqual(june.total_eos_accrual, Decimal("2250.00"))
        self.assertEqual(self.gl(june)["5210"], Decimal("2250.00"))
        self.assertEqual(self.gl(june)["2202"], Decimal("-2250.00"))
        self.emp.basic_salary = 9000  # زيادة الراتب تُسوّى بأثر رجعي على كامل المدة
        self.emp.save()
        july = self.payroll(month=7, submit=True)
        expected = (Decimal(7) / 12) * Decimal(12000) / 2 - Decimal("2250")
        self.assertEqual(july.total_eos_accrual, expected.quantize(Decimal("0.01")))
        with self.assertRaisesMessage(ValidationError, "بالترتيب الزمني"):
            self.payroll(month=5, submit=True)

    def settle(self, reason, last_day, submit=True, **kwargs):
        from hr.models import EndOfServiceSettlement
        s = EndOfServiceSettlement.objects.create(employee=self.emp, last_working_date=last_day, reason=reason,
                                                  posting_date=last_day, **kwargs)
        s.calculate_totals()
        s.save()
        if submit:
            s.submit()
        return s

    def test_settlement_resignation_reverses_excess_provision(self):
        self.emp.date_of_joining = datetime.date(YEAR - 3, 7, 1)
        self.emp.eos_opening_provision = Decimal("10000")
        self.emp.save()
        s = self.settle("resignation", d(6, 30), leave_days_override=Decimal("10"), other_deductions=500)
        self.assertEqual(s.service_years, 3)
        self.assertEqual(s.award, Decimal("4500.00"))  # 3 × 4500 × ⅓
        self.assertEqual(s.leave_encashment, Decimal("3000.00"))  # 10 أيام × 300
        self.assertEqual(s.net_amount, Decimal("7000.00"))
        entries = self.gl(s)
        self.assertEqual(entries["2202"], Decimal("10000.00"))  # إقفال المخصص كاملاً
        self.assertEqual(entries["5210"], Decimal("-5500.00"))  # رد المخصص الزائد
        self.assertEqual(entries["2103"], Decimal("-7000.00"))
        self.assertEqual(sum(entries.values()), 0)
        self.emp.refresh_from_db()
        self.assertEqual((self.emp.status, self.emp.relieving_date), ("left", d(6, 30)))
        # لا يدخل في مسيرات الأشهر التالية، ولا مخالصة ثانية
        self.assertFalse(self.payroll(month=7).lines.exists())
        with self.assertRaisesMessage(ValidationError, "مخالصة معتمدة"):
            self.settle("termination", d(6, 30))
        je = s.make_payment(self.user)
        self.assertEqual(je.total_debit, Decimal("7000.00"))
        je.submit()
        with self.assertRaisesMessage(ValidationError, "ألغِ قيد الصرف"):
            s.cancel()
        je.cancel()
        s.cancel()
        self.emp.refresh_from_db()
        self.assertEqual((self.emp.status, self.emp.relieving_date), ("active", None))

    def test_settlement_after_payroll_uses_accrued_provision(self):
        run = self.payroll(submit=True)
        s = self.settle("termination", d(6, 30))
        self.assertEqual(s.provision_balance, run.total_eos_accrual)
        self.assertEqual(s.award, Decimal("2250.00"))
        self.assertNotIn("5210", self.gl(s))  # المكافأة = المخصص تماماً
        # الإجازة السنوية: 21 × 181/365 يوماً تقريباً تُصرف تلقائياً
        self.assertEqual(s.leave_days, Decimal("10.41"))
        s.cancel()
        with self.assertRaisesMessage(ValidationError, "بعد آخر يوم عمل"):
            self.settle("termination", d(5, 31))

    def test_eos_pages(self):
        self.payroll(submit=True)
        page = self.client.get(reverse("doc_detail", args=["employee", self.emp.pk]))
        self.assertContains(page, "المكافأة عند الاستقالة")
        self.assertContains(self.client.get(reverse("eos_provision") + f"?as_of={YEAR}-06-30"), "2,250.00")
        s = self.settle("termination", d(6, 30))
        self.assertContains(self.client.get(s.get_absolute_url()), "تفاصيل احتساب المكافأة")
        self.assertEqual(self.client.get(reverse("doc_print", args=["eos-settlement", s.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse("payroll_settings")), "بداية الدوام")
        for slug in ("attendance", "eos-settlement"):
            self.assertEqual(self.client.get(reverse("doc_list", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_new", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_import_template", args=[slug, "xlsx"])).status_code, 200)


class LoansHolidaysShiftsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة السلف", "loanboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.emp = Employee.objects.create(
            employee_number="L1", full_name="مقترض", gender="male", date_of_joining=d(1, 1), device_id="77",
            basic_salary=3000, housing_allowance=1000, transport_allowance=500, gosi_subscribed=False)
        settings = PayrollSettings.for_company(self.company)
        settings.accrue_eos = False
        settings.save()
        from accounts.models import Account
        self.bank = Account.objects.get(number="1102")

    def loan(self, amount=2000, installment=800, month=6, submit=True):
        from hr.models import EmployeeLoan
        loan = EmployeeLoan.objects.create(employee=self.emp, amount=amount, installment_amount=installment,
                                           start_year=YEAR, start_month=month, payment_account=self.bank,
                                           posting_date=d(5, 20))
        if submit:
            loan.submit()
        return loan

    def payroll(self, month, submit=True):
        run = PayrollRun.objects.create(year=YEAR, month=month, posting_date=datetime.date(YEAR, month, 28))
        run.calculate_totals()
        run.save()
        if submit:
            run.submit()
        return run

    def balance(self, number):
        return sum(g.debit - g.credit for g in GLEntry.objects.filter(account__number=number, is_cancelled=False))

    # ── السلف ──
    def test_loan_installments_through_payroll(self):
        loan = self.loan()
        self.assertEqual(self.balance("1107"), Decimal("2000.00"))
        self.assertEqual(loan.installments_count, 3)
        self.assertFalse(self.payroll(5).lines.get().loan_deduction)  # يبدأ في يونيو
        june = self.payroll(6)
        line = june.lines.get()
        self.assertEqual((line.loan_deduction, line.net_pay), (Decimal("800.00"), Decimal("3700.00")))
        self.payroll(7)
        aug = self.payroll(8)
        self.assertEqual(aug.lines.get().loan_deduction, Decimal("400.00"))  # الباقي فقط
        loan.refresh_from_db()
        self.assertEqual(loan.outstanding, 0)
        self.assertEqual(self.balance("1107"), 0)
        self.assertEqual(self.payroll(9).lines.get().loan_deduction, 0)
        with self.assertRaisesMessage(ValidationError, "لا يمكن إلغاؤها"):
            loan.cancel()
        aug.cancel()  # إلغاء المسير يعيد القسط
        loan.refresh_from_db()
        self.assertEqual(loan.outstanding, Decimal("400.00"))

    def test_loan_capped_by_available_salary(self):
        self.loan(amount=10000, installment=8000)
        line = self.payroll(6).lines.get()
        self.assertEqual((line.loan_deduction, line.net_pay), (Decimal("4500.00"), Decimal("0.00")))

    def test_loan_validation(self):
        with self.assertRaisesMessage(ValidationError, "لا يتجاوز"):
            self.loan(amount=500, installment=800)

    def test_settlement_recovers_outstanding_loan(self):
        from hr.models import EndOfServiceSettlement
        loan = self.loan(amount=3000, installment=1000)
        self.payroll(6)
        s = EndOfServiceSettlement.objects.create(employee=self.emp, last_working_date=d(6, 30), reason="termination",
                                                  posting_date=d(6, 30), leave_days_override=0)
        s.calculate_totals()
        s.save()
        self.assertEqual(s.loan_deduction, Decimal("2000.00"))
        self.assertEqual(s.award, Decimal("1125.00"))  # نصف سنة × نصف شهر × 4500
        with self.assertRaisesMessage(ValidationError, "بالسالب"):
            s.submit()
        s.additions = Decimal("1000")
        s.save()
        s.submit()
        loan.refresh_from_db()
        self.assertEqual(loan.outstanding, 0)
        self.assertEqual(self.balance("1107"), 0)
        s.cancel()
        loan.refresh_from_db()
        self.assertEqual(loan.outstanding, Decimal("2000.00"))

    # ── العطل ──
    def test_holidays_excluded_from_leave_and_block_absence(self):
        from hr.models import Attendance, Holiday
        self.assertTrue(Holiday.objects.filter(name="اليوم الوطني").exists())
        Holiday.objects.create(name="عيد الفطر", from_date=d(3, 30), to_date=d(4, 2))
        annual = LeaveType.objects.get(name="إجازة سنوية")
        lv = LeaveApplication.objects.create(employee=self.emp, leave_type=annual, from_date=d(3, 28), to_date=d(4, 5))
        lv.submit()
        self.assertEqual(lv.total_days, 5)  # 9 أيام − 4 أيام عيد
        from hr.models import used_leave_days
        self.assertEqual(used_leave_days(self.emp, annual, YEAR), 5)
        with self.assertRaisesMessage(ValidationError, "عطلة رسمية"):
            Attendance.objects.create(employee=self.emp, attendance_date=d(9, 23), status="absent")
        worked = Attendance.objects.create(employee=self.emp, attendance_date=d(9, 23), check_in=datetime.time(10, 0),
                                           check_out=datetime.time(14, 0), overtime_hours=4)
        self.assertEqual(worked.late_minutes, 0)  # لا تأخير في العطلة

    def test_add_fixed_holidays_action(self):
        from hr.models import Holiday
        from core.hijri import eid_holidays
        Holiday.objects.all().delete()
        self.client.post(reverse("add_fixed_holidays"))
        expected = 2 + len(eid_holidays(YEAR))
        self.assertEqual(Holiday.objects.count(), expected)
        self.client.post(reverse("add_fixed_holidays"))
        self.assertEqual(Holiday.objects.count(), expected)
        self.assertContains(self.client.get(reverse("doc_list", args=["holiday"])), "إضافة العطل الرسمية")

    # ── الورديات ──
    def test_shift_drives_lateness_and_hourly_wage(self):
        from hr.models import Attendance, WorkShift
        shift = WorkShift.objects.create(name="مسائية", start_time=datetime.time(14, 0), end_time=datetime.time(22, 0),
                                         grace_minutes=10, hours_per_day=6)
        self.emp.shift = shift
        self.emp.save()
        a = Attendance.objects.create(employee=self.emp, attendance_date=d(6, 1), check_in=datetime.time(14, 20),
                                      check_out=datetime.time(21, 30))
        self.assertEqual((a.late_minutes, a.early_exit_minutes), (20, 30))
        settings = PayrollSettings.for_company(self.company)
        self.assertEqual(settings.hourly_wage(self.emp), Decimal(4500) / 30 / 6)

    # ── البصمة ──
    def test_punch_import(self):
        import io

        import openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        from hr.models import Attendance

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(["AC-No.", "Check Time"])
        sheet.append([77, datetime.datetime(YEAR, 6, 1, 8, 25)])
        sheet.append([77, datetime.datetime(YEAR, 6, 1, 12, 0)])
        sheet.append([77, datetime.datetime(YEAR, 6, 1, 17, 5)])
        sheet.append(["L1", f"{YEAR}-06-02 07:58"])  # بالرقم الوظيفي، بصمة واحدة
        sheet.append([999, datetime.datetime(YEAR, 6, 1, 8, 0)])
        buffer = io.BytesIO()
        book.save(buffer)
        upload = SimpleUploadedFile("punches.xlsx", buffer.getvalue())
        response = self.client.post(reverse("attendance_punches"), {"file": upload})
        self.assertContains(response, "999")
        first = Attendance.objects.get(attendance_date=d(6, 1))
        self.assertEqual((first.check_in, first.check_out, first.late_minutes),
                         (datetime.time(8, 25), datetime.time(17, 5), 25))
        second = Attendance.objects.get(attendance_date=d(6, 2))
        self.assertEqual((second.check_in, second.check_out), (datetime.time(7, 58), None))
        # إعادة الرفع لا تكرر السجلات
        upload = SimpleUploadedFile("punches.xlsx", buffer.getvalue())
        self.client.post(reverse("attendance_punches"), {"file": upload})
        self.assertEqual(Attendance.objects.count(), 2)

    def test_punch_import_csv_separate_columns(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from hr.models import Attendance

        csv_data = f"رقم البصمة,التاريخ,الوقت\n77,{YEAR}-06-03,08:00\n77,{YEAR}-06-03,16:00\n".encode()
        self.client.post(reverse("attendance_punches"), {"file": SimpleUploadedFile("p.csv", csv_data)})
        record = Attendance.objects.get()
        self.assertEqual((record.check_in, record.check_out, record.early_exit_minutes),
                         (datetime.time(8, 0), datetime.time(16, 0), 60))

    def test_pages(self):
        loan = self.loan()
        self.assertContains(self.client.get(loan.get_absolute_url()), "الاسترداد")
        self.assertContains(self.client.get(reverse("doc_detail", args=["employee", self.emp.pk])), "رصيد السلف")
        self.assertEqual(self.client.get(reverse("attendance_punches")).status_code, 200)
        run = self.payroll(6)
        self.assertContains(self.client.get(reverse("payslips", args=[run.pk])), "قسط سلفة")
        for slug in ("employee-loan", "holiday", "work-shift"):
            self.assertEqual(self.client.get(reverse("doc_list", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("doc_new", args=[slug])).status_code, 200)
        self.assertContains(self.client.get(reverse("attendance_monthly") + f"?year={YEAR}&month=9"), "ع")


class PortalAndShiftTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة البوابة", "portalboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.emp = Employee.objects.create(
            employee_number="P1", full_name="سارة المطيري", gender="female", date_of_joining=d(1, 1),
            basic_salary=6000, housing_allowance=1500, transport_allowance=500, gosi_subscribed=False, device_id="9")

    def make_portal_user(self):
        response = self.client.post(reverse("portal_access", args=[self.emp.pk]),
                                    {"action": "save", "username": "sara", "password": "Secret#123"})
        self.assertEqual(response.status_code, 302)
        self.emp.refresh_from_db()
        return self.emp.user

    # ── البوابة ──
    def test_portal_account_does_not_count_and_is_isolated(self):
        from saas.services import active_user_count
        before = active_user_count(self.company)
        user = self.make_portal_user()
        self.assertEqual(active_user_count(self.company), before)
        self.assertNotContains(self.client.get(reverse("users_list")), '<td dir="ltr" style="text-align:right">sara</td>', html=False)
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("portal_home")), "سارة المطيري")
        # لا يصل لأي صفحة أخرى في النظام
        for url in (reverse("dashboard"), reverse("doc_list", args=["employee"]), reverse("hr_dashboard"),
                    reverse("payroll_settings"), reverse("doc_list", args=["sales-invoice"])):
            response = self.client.get(url)
            self.assertRedirects(response, reverse("portal_home"), fetch_redirect_response=False)

    def test_leave_request_approve_and_reject(self):
        user = self.make_portal_user()
        self.client.force_login(user)
        annual = LeaveType.objects.get(name="إجازة سنوية")
        response = self.client.post(reverse("portal_leave"), {
            "leave_type": annual.pk, "from_date": f"{YEAR}-07-01", "to_date": f"{YEAR}-07-30", "remarks": "سفر"})
        self.assertContains(response, "لا يكفي")  # 30 يوماً > 21
        self.client.post(reverse("portal_leave"), {
            "leave_type": annual.pk, "from_date": f"{YEAR}-07-01", "to_date": f"{YEAR}-07-05", "remarks": "سفر"})
        leave = LeaveApplication.objects.get()
        self.assertTrue(leave.is_draft and leave.requested_via_portal)
        self.assertContains(self.client.get(reverse("portal_home")), "بانتظار الاعتماد")
        # المدير يرى الطلب في لوحة الموارد البشرية ويعتمده
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("hr_dashboard")), leave.name)
        self.client.post(reverse("doc_action", args=["leave-application", leave.pk, "submit"]))
        leave.refresh_from_db()
        self.assertTrue(leave.is_submitted)
        # طلب ثانٍ يُرفض مع السبب
        self.client.force_login(user)
        self.client.post(reverse("portal_leave"), {
            "leave_type": annual.pk, "from_date": f"{YEAR}-08-01", "to_date": f"{YEAR}-08-02"})
        second = LeaveApplication.objects.get(docstatus=DocStatus.DRAFT)
        self.client.force_login(self.user)
        self.client.post(reverse("doc_action", args=["leave-application", second.pk, "reject"]),
                         {"reason": "ضغط عمل"})
        second.refresh_from_db()
        self.assertTrue(second.is_rejected)
        self.client.force_login(user)
        page = self.client.get(reverse("portal_home"))
        self.assertContains(page, "مرفوض")
        self.assertContains(page, "ضغط عمل")

    def test_loan_request_withdraw_and_payslip_access(self):
        user = self.make_portal_user()
        other = Employee.objects.create(employee_number="P2", full_name="آخر", gender="male", date_of_joining=d(1, 1),
                                        basic_salary=3000, gosi_subscribed=False)
        run = PayrollRun.objects.create(year=YEAR, month=6, posting_date=d(6, 28))
        run.calculate_totals()
        run.save()
        run.submit()
        self.client.force_login(user)
        self.client.post(reverse("portal_loan"), {"amount": "3000", "installment_amount": "500", "start_month": 8,
                                                  "start_year": YEAR, "reason": "ظرف طارئ"})
        from hr.models import EmployeeLoan
        loan = EmployeeLoan.objects.get()
        self.assertTrue(loan.is_draft and loan.requested_via_portal)
        self.client.post(reverse("portal_withdraw", args=["loan", loan.pk]))
        self.assertFalse(EmployeeLoan.objects.exists())
        mine = run.lines.get(employee=self.emp)
        self.assertContains(self.client.get(reverse("portal_payslip", args=[mine.pk])), "قسيمة راتب")
        theirs = run.lines.get(employee=other)
        self.assertEqual(self.client.get(reverse("portal_payslip", args=[theirs.pk])).status_code, 404)

    def test_portal_disable_blocks_login(self):
        user = self.make_portal_user()
        self.client.post(reverse("portal_access", args=[self.emp.pk]), {"action": "disable"})
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("portal_home")).status_code, 403)

    # ── اللوحة ──
    def test_dashboard_expiring_documents(self):
        self.emp.id_expiry_date = datetime.date.today() + datetime.timedelta(days=20)
        self.emp.passport_expiry_date = datetime.date.today() - datetime.timedelta(days=3)
        self.emp.save()
        page = self.client.get(reverse("hr_dashboard"))
        self.assertContains(page, "20 يوم")
        self.assertContains(page, "منتهية منذ 3 يوم")

    # ── الورديات الليلية والتدوير ──
    def night_shift(self):
        from hr.models import WorkShift
        return WorkShift.objects.create(name="ليلية", start_time=datetime.time(22, 0), end_time=datetime.time(6, 0),
                                        grace_minutes=10, hours_per_day=8)

    def test_night_shift_attendance(self):
        from hr.models import Attendance
        self.emp.shift = self.night_shift()
        self.emp.save()
        a = Attendance.objects.create(employee=self.emp, attendance_date=d(6, 1), check_in=datetime.time(0, 30),
                                      check_out=datetime.time(5, 30))
        self.assertEqual((a.late_minutes, a.early_exit_minutes, a.worked_hours), (150, 30, Decimal("5.00")))
        b = Attendance.objects.create(employee=self.emp, attendance_date=d(6, 2), check_in=datetime.time(21, 55),
                                      check_out=datetime.time(6, 5))
        self.assertEqual((b.late_minutes, b.early_exit_minutes, b.worked_hours), (0, 0, Decimal("8.17")))

    def test_night_shift_punch_import_and_rotation(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from hr.models import Attendance, ShiftAssignment
        ShiftAssignment.objects.create(employee=self.emp, shift=self.night_shift(), from_date=d(6, 1), to_date=d(6, 30))
        csv_data = (f"AC-No.,Check Time\n9,{YEAR}-06-10 21:58\n9,{YEAR}-06-11 06:03\n"
                    f"9,{YEAR}-07-01 08:20\n9,{YEAR}-07-01 17:00\n").encode()
        self.client.post(reverse("attendance_punches"), {"file": SimpleUploadedFile("p.csv", csv_data)})
        night = Attendance.objects.get(attendance_date=d(6, 10))  # بصمة الصباح تتبع ليلة 10 يونيو
        self.assertEqual((night.check_in, night.check_out, night.late_minutes), (datetime.time(21, 58), datetime.time(6, 3), 0))
        day = Attendance.objects.get(attendance_date=d(7, 1))  # بعد انتهاء التكليف يعود للدوام العام
        self.assertEqual(day.late_minutes, 20)
        self.assertEqual(Attendance.objects.count(), 2)
        with self.assertRaisesMessage(ValidationError, "تتداخل"):
            ShiftAssignment(employee=self.emp, shift=ShiftAssignment.objects.first().shift,
                            from_date=d(6, 15), to_date=d(7, 15)).full_clean()


class HijriAppraisalNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة التقييم", "apprboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.emp = Employee.objects.create(employee_number="H1", full_name="هند", gender="female",
                                           date_of_joining=d(1, 1), basic_salary=5000, gosi_subscribed=False)

    # ── الهجري ──
    def test_hijri_conversion_and_eids(self):
        from core.hijri import eid_holidays, format_hijri
        self.assertEqual(format_hijri(datetime.date(2026, 3, 20)), "1 شوال 1447هـ")
        self.assertEqual(eid_holidays(2026), [
            ("عيد الفطر 1447هـ", datetime.date(2026, 3, 19), datetime.date(2026, 3, 22)),
            ("عيد الأضحى 1447هـ", datetime.date(2026, 5, 26), datetime.date(2026, 5, 29)),
        ])
        # الشركة الجديدة تحصل على العيدين ضمن عطلها
        from hr.models import Holiday
        self.assertTrue(Holiday.objects.filter(name__startswith="عيد الفطر").exists() or not eid_holidays(YEAR))

    # ── التقييم ──
    def appraisal(self, scores=None):
        from hr.models import Appraisal, AppraisalTemplate
        a = Appraisal.objects.create(employee=self.emp, template=AppraisalTemplate.objects.get(),
                                     period_from=d(1, 1), period_to=d(6, 30))
        a.calculate_totals()
        a.save()
        for score, value in zip(a.scores.all(), scores or []):
            score.score = value
            score.save()
        a.calculate_totals()
        a.save()
        return a

    def test_appraisal_weighted_score(self):
        a = self.appraisal()
        self.assertEqual(a.scores.count(), 6)
        with self.assertRaisesMessage(ValidationError, "أدخل درجة"):
            a.submit()
        a = self.appraisal([5, 4, 5, 3, 4, 5])
        # (25×5 + 25×4 + 15×5 + 15×3 + 10×4 + 10×5) / 100 / 5 = 87%
        self.assertEqual((a.score_percent, a.rating), (Decimal("87.0"), "جيد جداً"))
        a.submit()
        self.assertTrue(a.is_submitted)

    def test_appraisal_weights_must_total_100(self):
        a = self.appraisal([3] * 6)
        first = a.scores.first()
        first.weight = 50
        first.save()
        with self.assertRaisesMessage(ValidationError, "100"):
            a.submit()

    def test_appraisal_form_fetches_criteria(self):
        from hr.models import Appraisal, AppraisalTemplate
        response = self.client.post(reverse("doc_new", args=["appraisal"]), {
            "employee": self.emp.pk, "template": AppraisalTemplate.objects.get().pk,
            "period_from": f"{YEAR}-01-01", "period_to": f"{YEAR}-06-30", "posting_date": f"{YEAR}-07-01",
            "scores-TOTAL_FORMS": 0, "scores-INITIAL_FORMS": 0, "scores-MIN_NUM_FORMS": 0, "scores-MAX_NUM_FORMS": 1000,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appraisal.objects.get().scores.count(), 6)

    # ── التنبيهات وكلمة المرور ──
    def portal_user(self):
        self.client.post(reverse("portal_access", args=[self.emp.pk]),
                         {"action": "save", "username": "hind", "password": "Secret#123"})
        self.emp.refresh_from_db()
        return self.emp.user

    def test_request_notifications_flow(self):
        from core.models import Notification
        user = self.portal_user()
        self.client.force_login(user)
        annual = LeaveType.objects.get(name="إجازة سنوية")
        self.client.post(reverse("portal_leave"), {"leave_type": annual.pk, "from_date": f"{YEAR}-07-01",
                                                   "to_date": f"{YEAR}-07-02"})
        manager_note = Notification.objects.get(user=self.user)
        self.assertIn("هند", manager_note.title)
        self.client.force_login(self.user)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, 'class="bell"')
        response = self.client.get(reverse("notification_open", args=[manager_note.pk]))
        leave = LeaveApplication.objects.get()
        self.assertRedirects(response, leave.get_absolute_url(), fetch_redirect_response=False)
        manager_note.refresh_from_db()
        self.assertTrue(manager_note.is_read)
        self.client.post(reverse("doc_action", args=["leave-application", leave.pk, "submit"]))
        employee_note = Notification.objects.get(user=user)
        self.assertIn("اعتُمد", employee_note.title)
        # الموظف يصل لتنبيهاته وتغيير كلمة مروره
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("notifications")), "اعتُمد")
        response = self.client.post(reverse("password_change"), {
            "old_password": "Secret#123", "new_password1": "Another#4567", "new_password2": "Another#4567"})
        self.assertRedirects(response, reverse("password_change_done"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Another#4567"))

    def test_reminders_command(self):
        from django.core.management import call_command
        from core.models import Notification
        self.emp.id_expiry_date = datetime.date.today() + datetime.timedelta(days=10)
        self.emp.save()
        call_command("hr_reminders", stdout=io.StringIO())
        self.assertEqual(Notification.objects.filter(user=self.user, title__contains="الهوية").count(), 1)
        call_command("hr_reminders", stdout=io.StringIO())  # لا يتكرر
        self.assertEqual(Notification.objects.filter(user=self.user, title__contains="الهوية").count(), 1)

    def test_email_disabled_by_default(self):
        from django.conf import settings
        from django.core import mail
        self.assertFalse(settings.SYNAR_EMAIL_ENABLED)
        self.user.email = "boss@example.com"
        self.user.save()
        from core.notify import notify_managers
        notify_managers(self.company, "اختبار")
        self.assertEqual(len(mail.outbox), 0)


class DevicePushTests(TestCase):
    """محاكاة جهاز ZKTeco يرسل البصمات بروتوكول ADMS كما يرسلها الجهاز الحقيقي."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الأجهزة", "deviceboss")
        cls.other, _ = make_company("شركة أخرى للأجهزة", "otherdev")

    def setUp(self):
        from hr.models import AttendanceDevice
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.emp = Employee.objects.create(employee_number="E7", full_name="خالد", gender="male", device_id="7",
                                           date_of_joining=d(1, 1), basic_salary=5000, gosi_subscribed=False)
        self.device = AttendanceDevice.objects.create(name="البوابة", serial_number="CQZ7232560123")

    def push(self, lines, sn="CQZ7232560123", ip="127.0.0.1"):
        body = "\n".join(lines)
        return self.client.post(f"/iclock/cdata?SN={sn}&table=ATTLOG&Stamp=9999", data=body,
                                content_type="text/plain", REMOTE_ADDR=ip)

    def test_handshake_and_options(self):
        response = self.client.get("/iclock/cdata?SN=CQZ7232560123&options=all&pushver=2.4.1&language=69")
        self.assertEqual(response.status_code, 200)
        self.assertIn("GET OPTION FROM: CQZ7232560123", response.content.decode())
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_online)
        self.assertEqual(self.device.firmware, "2.4.1")
        self.assertEqual(self.client.get("/iclock/getrequest?SN=CQZ7232560123").content, b"OK")

    def test_realtime_punches_build_attendance(self):
        from hr.models import Attendance, AttendancePunch
        r = self.push([f"7\t{YEAR}-06-01 08:25:13\t0\t1\t0\t0\t0"])  # حضور
        self.assertEqual(r.content, b"OK: 1")
        record = Attendance.objects.get(employee=self.emp, attendance_date=d(6, 1))
        self.assertEqual((record.check_in, record.check_out, record.late_minutes), (datetime.time(8, 25), None, 25))
        self.push([f"7\t{YEAR}-06-01 12:01:00\t2\t1\t0\t0", f"7\t{YEAR}-06-01 17:03:40\t1\t1\t0\t0"])
        record.refresh_from_db()
        self.assertEqual((record.check_in, record.check_out), (datetime.time(8, 25), datetime.time(17, 3)))
        # الجهاز يعيد إرسال نفس الحركات بعد انقطاع: لا تكرار
        self.push([f"7\t{YEAR}-06-01 08:25:13\t0\t1\t0\t0"])
        self.assertEqual(AttendancePunch.objects.count(), 3)
        self.assertEqual(AttendancePunch.objects.first().device, self.device)

    def test_unknown_and_disabled_devices_rejected(self):
        from hr.models import AttendanceDevice, AttendancePunch
        self.assertEqual(self.push([f"7\t{YEAR}-06-01 08:00:00"], sn="FAKE123").status_code, 401)
        self.device.allowed_ips = "10.0.0.5"
        self.device.save()
        self.assertEqual(self.push([f"7\t{YEAR}-06-01 08:00:00"], ip="1.2.3.4").status_code, 401)
        self.assertEqual(self.push([f"7\t{YEAR}-06-01 08:00:00"], ip="10.0.0.5").status_code, 200)
        AttendanceDevice.objects.update(enabled=False)
        self.assertEqual(self.push([f"7\t{YEAR}-06-02 08:00:00"]).status_code, 401)
        self.assertEqual(AttendancePunch.objects.count(), 1)

    def test_punches_stay_in_device_company(self):
        from hr.models import AttendancePunch
        self.push([f"7\t{YEAR}-06-01 08:00:00"])
        with tenant_context(self.other):
            self.assertFalse(AttendancePunch.objects.exists())
        self.assertEqual(AttendancePunch.objects.get().company, self.company)

    def test_unknown_pin_then_linked_and_reprocessed(self):
        from hr.models import Attendance
        newcomer = Employee.objects.create(employee_number="E9", full_name="سلمى", gender="female",
                                           date_of_joining=d(1, 1), basic_salary=4000, gosi_subscribed=False)
        self.push([f"55\t{YEAR}-06-03 07:59:00", f"55\t{YEAR}-06-03 16:00:00"])
        self.assertFalse(Attendance.objects.filter(employee=newcomer).exists())
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("attendance_devices")), "55")
        newcomer.device_id = "55"
        newcomer.save()
        self.client.post(reverse("attendance_devices"), {"action": "reprocess"})
        record = Attendance.objects.get(employee=newcomer)
        self.assertEqual((record.check_in, record.check_out), (datetime.time(7, 59), datetime.time(16, 0)))

    def test_other_tables_acknowledged(self):
        r = self.client.post("/iclock/cdata?SN=CQZ7232560123&table=OPERLOG", data="OPLOG 4\t0\t2026-06-01",
                             content_type="text/plain")
        self.assertEqual(r.content, b"OK")

    def test_device_pages(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("attendance_devices"))
        self.assertContains(page, "CQZ7232560123")
        self.client.post(reverse("attendance_devices"), {"name": "الفرع", "serial_number": "NEWSN1", "enabled": "on"})
        from hr.models import AttendanceDevice
        self.assertTrue(AttendanceDevice.objects.filter(serial_number="NEWSN1").exists())
        self.push([f"7\t{YEAR}-06-01 08:00:00"])
        self.assertContains(self.client.get(reverse("raw_punches")), "خالد")
