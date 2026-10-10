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
