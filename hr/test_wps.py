import csv
import io
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.tenancy import tenant_context
from hr.models import Employee, PayrollRun
from hr.tests import JUNE_END, YEAR, d
from hr.wps import bank_code
from saas.tests import make_company


class WPSFileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة حماية الأجور", "wpsboss")

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

    def payroll(self, submit=False):
        run = PayrollRun.objects.create(year=YEAR, month=6, posting_date=JUNE_END)
        run.calculate_totals()
        run.save()
        if submit:
            run.submit()
        return run

    def test_bank_codes(self):
        self.assertEqual(bank_code("SA03 8000 0000 6080 1016 7519"), "RJHI")
        self.assertEqual(bank_code("SA4410000000000000000001"), "NCBK")
        self.assertEqual(bank_code("AE070331234567890123456"), "")

    def test_wps_file_rows_and_validation(self):
        Employee.objects.filter(pk=self.saudi.pk).update(national_id="1012345678")
        run = self.payroll(submit=True)
        response = self.client.get(reverse("payroll_wps_file", args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertIn("IBAN", rows[0][3])
        by_no = {row[9]: row for row in rows[1:]}
        saudi, expat = by_no["E1"], by_no["E2"]
        self.assertEqual((saudi[1], saudi[2], saudi[10]), ("1012345678", "RJHI", ""))
        # الصافي = الأساسي + السكن + الأخرى − الاستقطاعات
        net, basic, housing, other, deductions = (Decimal(saudi[i]) for i in (4, 5, 6, 7, 8))
        self.assertEqual(net, basic + housing + other - deductions)
        self.assertEqual(net, run.lines.get(employee=self.saudi).net_pay)
        self.assertIn("آيبان", expat[10])
        self.assertEqual(response["X-WPS-Problems"], "1")
        self.assertContains(self.client.get(run.get_absolute_url()), "ملف حماية الأجور")

    def test_wps_requires_submitted_run(self):
        run = self.payroll()
        response = self.client.get(reverse("payroll_wps_file", args=[run.pk]))
        self.assertEqual(response.status_code, 302)
