import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import Membership
from core.tenancy import tenant_context
from hr.models import (Appraisal, AppraisalTemplate, Attendance, Employee, KPIDirection, KPISheet, kpi_achievement)
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse

TODAY = datetime.date.today()
START = TODAY.replace(day=1)


class KPIAchievementTests(TestCase):
    def test_formula(self):
        self.assertEqual(kpi_achievement(100, 80, KPIDirection.HIGHER), Decimal("80.0"))
        self.assertEqual(kpi_achievement(100, 500, KPIDirection.HIGHER), Decimal("120.0"))     # الحد الأقصى
        self.assertEqual(kpi_achievement(60, 120, KPIDirection.LOWER), Decimal("50.0"))
        self.assertEqual(kpi_achievement(60, 0, KPIDirection.LOWER), Decimal("120.0"))
        self.assertEqual(kpi_achievement(0, 0, KPIDirection.LOWER), Decimal("100.0"))
        self.assertEqual(kpi_achievement(0, 3, KPIDirection.LOWER), Decimal("0.0"))


class KPISheetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الأهداف", "kpiboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.emp = Employee.objects.create(employee_number="K1", full_name="سارة", gender="female",
                                           date_of_joining=START - datetime.timedelta(days=400), basic_salary=6000,
                                           gosi_subscribed=False)
        Membership.objects.filter(company=self.company, user=self.user).update(employee=self.emp)

    def sell(self, rate, user=None):
        item = Item.objects.get_or_create(code="K-ITEM", defaults={
            "name": "صنف", "item_group": ItemGroup.objects.first(), "uom": UOM.objects.first(),
            "is_stock_item": False})[0]
        inv = SalesInvoice.objects.create(customer=Customer.objects.get_or_create(name="عميل")[0],
                                          warehouse=Warehouse.objects.first(), update_stock=False,
                                          created_by=user or self.user, posting_date=START)
        inv.items.create(item=item, qty=1, rate=rate)
        inv.submit()
        return inv

    def attend(self, day, status="present", late=0):
        record = Attendance.objects.create(employee=self.emp, attendance_date=START + datetime.timedelta(days=day),
                                           status=status)
        Attendance.objects.filter(pk=record.pk).update(late_minutes=late)

    def sheet(self, lines):
        sheet = KPISheet.objects.create(employee=self.emp, period_from=START, period_to=START + datetime.timedelta(days=27))
        for line in lines:
            sheet.lines.create(**line)
        sheet.calculate_totals()
        sheet.save()
        return sheet

    def test_automatic_measures_and_weighted_result(self):
        self.sell(6000)
        self.sell(2000)
        other = make_company("شركة أخرى", "otheruser")[1]
        self.sell(9999, user=other)                                         # مستخدم غير مرتبط بالموظف
        self.attend(0, late=30)
        self.attend(1, late=30)
        self.attend(2, status="half_day")
        self.attend(3, status="absent")
        sheet = self.sheet([
            {"kpi": "المبيعات", "measure": "sales_amount", "target": 10000, "weight": 40},
            {"kpi": "عدد الفواتير", "measure": "sales_count", "target": 2, "weight": 10},
            {"kpi": "الحضور", "measure": "attendance_rate", "target": 100, "weight": 20},
            {"kpi": "التأخير", "measure": "late_minutes", "target": 30, "weight": 10},
            {"kpi": "رضا العملاء", "measure": "manual", "target": 90, "actual": 81, "weight": 20},
        ])
        lines = {line.kpi: line for line in sheet.lines.all()}
        self.assertEqual(lines["المبيعات"].actual, Decimal("8000"))
        self.assertEqual(lines["المبيعات"].achievement, Decimal("80.0"))
        self.assertEqual(lines["عدد الفواتير"].achievement, Decimal("100.0"))
        self.assertEqual(lines["الحضور"].actual, Decimal("62.5"))             # (2 + 0.5) ÷ 4
        self.assertEqual(lines["التأخير"].direction, "lower")
        self.assertEqual(lines["التأخير"].achievement, Decimal("50.0"))        # 30 ÷ 60
        self.assertEqual(lines["رضا العملاء"].achievement, Decimal("90.0"))
        # 40×80 + 10×100 + 20×62.5 + 10×50 + 20×90 = 77.5%
        self.assertEqual((sheet.achievement_percent, sheet.rating), (Decimal("77.5"), "جيد"))
        sheet.submit()
        self.sell(5000)                                                     # بعد الاعتماد لا تتغير النتيجة
        sheet.refresh_from_db()
        self.assertEqual(sheet.lines.get(kpi="المبيعات").actual, Decimal("8000"))

    def test_validation(self):
        sheet = self.sheet([{"kpi": "المبيعات", "measure": "sales_amount", "target": 1000, "weight": 60}])
        with self.assertRaisesMessage(ValidationError, "100"):
            sheet.submit()
        manual = self.sheet([{"kpi": "مشروع", "target": 1, "weight": 100}])
        with self.assertRaisesMessage(ValidationError, "الفعلية"):
            manual.submit()

    def test_appraisal_includes_kpi_result(self):
        self.sell(12000)
        kpis = self.sheet([{"kpi": "المبيعات", "measure": "sales_amount", "target": 10000, "weight": 100}])
        self.assertEqual(kpis.achievement_percent, Decimal("120.0"))
        appraisal = Appraisal.objects.create(employee=self.emp, template=AppraisalTemplate.objects.get(),
                                             period_from=START, period_to=START + datetime.timedelta(days=27),
                                             kpi_weight=40)
        appraisal.calculate_totals()
        appraisal.save()
        appraisal.scores.update(score=3)
        appraisal.calculate_totals()
        with self.assertRaisesMessage(ValidationError, "أهداف أداء معتمدة"):
            appraisal.submit()
        kpis.submit()
        appraisal.calculate_totals()
        appraisal.save()
        # المعايير 60% × 60 + الأهداف (حدها 100%) × 40
        self.assertEqual(appraisal.kpi_sheet, kpis)
        self.assertEqual((appraisal.criteria_percent, appraisal.score_percent), (Decimal("60.0"), Decimal("76.0")))
        appraisal.submit()

    def test_pages_portal_and_user_link(self):
        response = self.client.post(reverse("doc_new", args=["kpi-sheet"]), {
            "employee": self.emp.pk, "period_from": START.isoformat(), "period_to": TODAY.isoformat(),
            "posting_date": TODAY.isoformat(), "remarks": "",
            "lines-TOTAL_FORMS": 1, "lines-INITIAL_FORMS": 0, "lines-MIN_NUM_FORMS": 1, "lines-MAX_NUM_FORMS": 1000,
            "lines-0-kpi": "الغياب", "lines-0-measure": "absent_days", "lines-0-direction": "higher",
            "lines-0-target": "0", "lines-0-weight": "100",
        })
        sheet = KPISheet.objects.get()
        self.assertRedirects(response, sheet.get_absolute_url())
        self.assertEqual(sheet.achievement_percent, Decimal("100.0"))
        self.assertContains(self.client.get(sheet.get_absolute_url()), "نسبة التحقيق")
        self.client.post(reverse("doc_new", args=["appraisal"]), {
            "employee": self.emp.pk, "template": AppraisalTemplate.objects.get().pk, "kpi_weight": "",
            "period_from": START.isoformat(), "period_to": TODAY.isoformat(), "posting_date": TODAY.isoformat(),
            "scores-TOTAL_FORMS": 0, "scores-INITIAL_FORMS": 0, "scores-MIN_NUM_FORMS": 0, "scores-MAX_NUM_FORMS": 1000,
        })
        self.assertEqual(Appraisal.objects.get().kpi_weight, 0)             # الوزن الفارغ = صفر
        # ربط مستخدم بموظف من صفحة المستخدمين
        member = Membership.objects.create(user=make_company("شركة ثالثة", "seller")[1], company=self.company)
        self.client.post(reverse("user_edit", args=[member.pk]), {"role": "user", "can_submit": "on",
                                                                  "employee": self.emp.pk})
        member.refresh_from_db()
        self.assertEqual(member.employee, self.emp)
        # بوابة الموظف
        self.client.post(reverse("portal_access", args=[self.emp.pk]),
                         {"action": "save", "username": "sara", "password": "Secret#123"})
        self.emp.refresh_from_db()
        self.client.force_login(self.emp.user)
        self.assertContains(self.client.get(reverse("portal_home")), "أهدافي")
