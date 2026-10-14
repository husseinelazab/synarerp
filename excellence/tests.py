import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.models import Notification
from core.tenancy import tenant_context
from excellence.models import (KPI, Initiative, InitiativeStatus, KPIDirection, KPIReading, KPIStatus, PlanStatus,
                               StrategicGoal, StrategicPlan, achievement_percent, achievement_status)

TODAY = timezone.localdate()
DAY = datetime.timedelta(days=1)


class ExcellenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية التميز", "tamayuz")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.plan = StrategicPlan.objects.create(title="الخطة الاستراتيجية 2026–2030", start_year=2026, end_year=2030,
                                                 vision="ريادة في التنمية", mission="خدمة المستفيدين",
                                                 values="الشفافية\nالإتقان")
        self.g1 = StrategicGoal.objects.create(plan=self.plan, code="ه1", title="تمكين المستفيدين", weight=60,
                                               vision2030_pillar="society", sdg=1, owner=self.user)
        self.g2 = StrategicGoal.objects.create(plan=self.plan, code="ه2", title="الاستدامة المالية", weight=40,
                                               vision2030_pillar="economy", sdg=17)

    def initiative(self, goal=None, **extra):
        data = {"goal": goal or self.g1, "title": "مبادرة", "start_date": TODAY - 30 * DAY,
                "end_date": TODAY + 30 * DAY, **extra}
        return Initiative.objects.create(**data)

    def kpi(self, **extra):
        data = {"goal": self.g1, "name": "نسبة رضا المستفيدين", "target": Decimal(80), **extra}
        return KPI.objects.create(**data)

    # ───── الحسابات ─────

    def test_progress_rollups(self):
        self.initiative(progress=40, weight=1)
        self.initiative(progress=100, weight=3)  # تكتمل تلقائياً
        self.initiative(progress=0, status=InitiativeStatus.CANCELLED)  # لا تدخل في الحساب
        self.initiative(goal=self.g2, progress=50)
        self.assertEqual(self.g1.progress, Decimal("85.0"))  # (40×1 + 100×3) ÷ 4
        self.assertEqual(self.g2.progress, Decimal("50.0"))
        self.assertEqual(self.plan.progress, Decimal("71.0"))  # 85×60% + 50×40%
        self.assertEqual(Initiative.objects.filter(status=InitiativeStatus.COMPLETED).count(), 1)

    def test_goal_default_equal_weights_and_limits(self):
        self.g2.weight = None
        self.g2.save()
        g3 = StrategicGoal.objects.create(plan=self.plan, code="ه3", title="الحوكمة")
        self.assertEqual(self.g2.effective_weight, Decimal("20.0"))
        self.assertEqual(g3.effective_weight, Decimal("20.0"))
        with self.assertRaises(ValidationError):
            StrategicGoal(plan=self.plan, code="ه4", title="زائد", weight=50).full_clean()
        with self.assertRaises(ValidationError):
            StrategicGoal(plan=self.plan, code="ه1", title="مكرر").full_clean()

    def test_achievement_both_directions_and_status(self):
        self.assertEqual(achievement_percent(72, 80, KPIDirection.HIGHER), Decimal("90.0"))
        self.assertEqual(achievement_percent(400, 100, KPIDirection.HIGHER), Decimal("150.0"))  # سقف العرض
        self.assertEqual(achievement_percent(10, 8, KPIDirection.LOWER), Decimal("80.0"))
        self.assertEqual(achievement_percent(0, 8, KPIDirection.LOWER), Decimal("150.0"))  # لا قسمة على صفر
        self.assertEqual(achievement_percent(5, 0, KPIDirection.HIGHER), Decimal("100.0"))
        self.assertEqual(achievement_status(Decimal(90)), KPIStatus.GREEN)
        self.assertEqual(achievement_status(Decimal("89.9")), KPIStatus.YELLOW)
        self.assertEqual(achievement_status(Decimal(70)), KPIStatus.YELLOW)
        self.assertEqual(achievement_status(Decimal(69)), KPIStatus.RED)
        self.assertEqual(achievement_status(None), KPIStatus.GREY)

        kpi = self.kpi()
        self.assertEqual(kpi.status, KPIStatus.GREY)
        KPIReading.objects.create(kpi=kpi, period_end=TODAY - 100 * DAY, value=40)
        KPIReading.objects.create(kpi=kpi, period_end=TODAY - 10 * DAY, value=60)
        self.assertEqual(kpi.latest_reading.value, 60)
        self.assertEqual(kpi.achievement, Decimal("75.0"))
        self.assertEqual(kpi.status, KPIStatus.YELLOW)
        lower = self.kpi(name="متوسط مدة دراسة الحالة", unit="days", target=5, direction=KPIDirection.LOWER)
        KPIReading.objects.create(kpi=lower, period_end=TODAY, value=10)
        self.assertEqual(lower.status, KPIStatus.RED)

    def test_delayed_detection(self):
        late = self.initiative(end_date=TODAY - DAY, progress=60)
        self.assertTrue(late.is_delayed)
        self.assertFalse(self.initiative(end_date=TODAY - DAY, progress=100).is_delayed)
        self.assertFalse(self.initiative(end_date=TODAY - DAY, status=InitiativeStatus.CANCELLED).is_delayed)
        self.assertFalse(self.initiative(end_date=TODAY + DAY, progress=10).is_delayed)

    def test_plan_actions_and_overlap(self):
        with self.assertRaises(ValidationError):
            StrategicPlan(title="خطأ", start_year=2030, end_year=2026).full_clean()
        self.plan.approve(self.user)
        self.assertEqual(self.plan.status, PlanStatus.APPROVED)
        other = StrategicPlan.objects.create(title="خطة متداخلة", start_year=2028, end_year=2032)
        StrategicGoal.objects.create(plan=other, code="ه1", title="هدف")
        with self.assertRaisesMessage(ValidationError, "تتداخل"):
            other.approve(self.user)
        self.plan.close(self.user)
        other.approve(self.user)
        self.assertEqual(StrategicPlan.current(), other)

    # ───── الصفحات ─────

    def test_kpi_entry_creates_and_updates_readings(self):
        self.client.force_login(self.user)
        k1, k2 = self.kpi(), self.kpi(name="عدد المستفيدين", unit="count", target=500)
        period = TODAY.replace(day=1) - DAY
        url = f"/excellence/kpi-entry/?plan={self.plan.pk}&period_end={period:%Y-%m-%d}"
        self.assertContains(self.client.get(url), "عدد المستفيدين")
        response = self.client.post(url, {"period_end": f"{period:%Y-%m-%d}", f"value_{k1.pk}": "70",
                                          f"note_{k1.pk}": "استبيان", f"value_{k2.pk}": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(KPIReading.objects.get(kpi=k1, period_end=period).value, 70)
        self.assertFalse(KPIReading.objects.filter(kpi=k2).exists())
        self.client.post(url, {"period_end": f"{period:%Y-%m-%d}", f"value_{k1.pk}": "85", f"value_{k2.pk}": "520"})
        self.assertEqual(KPIReading.objects.get(kpi=k1, period_end=period).value, 85)
        self.assertEqual(KPIReading.objects.filter(kpi=k1).count(), 1)
        self.assertEqual(KPIReading.objects.get(kpi=k2).value, 520)
        response = self.client.post(url, {"period_end": f"{period:%Y-%m-%d}", f"value_{k1.pk}": "abc"})
        self.assertContains(response, "قيمة غير صحيحة")

    def test_alerts_command(self):
        self.plan.approve(self.user)
        late = self.initiative(title="بناء نظام التطوع", end_date=TODAY - 2 * DAY, progress=50, owner=self.user)
        self.initiative(title="مبادرة قريبة", end_date=TODAY + 10 * DAY, progress=20)
        self.initiative(title="مبادرة بعيدة", end_date=TODAY + 60 * DAY, progress=20)
        stale = self.kpi(owner=self.user, frequency="monthly")
        KPIReading.objects.create(kpi=stale, period_end=TODAY - 50 * DAY, value=80)
        fresh = self.kpi(name="مؤشر محدث", owner=self.user)
        KPIReading.objects.create(kpi=fresh, period_end=TODAY - 20 * DAY, value=80)
        call_command("excellence_alerts", stdout=open("/dev/null", "w"))
        titles = list(Notification.objects.filter(user=self.user).values_list("title", flat=True))
        self.assertTrue(any("مبادرة متأخرة" in t and "بناء نظام التطوع" in t for t in titles))
        self.assertTrue(any("مبادرة قريبة" in t for t in titles))
        self.assertFalse(any("مبادرة بعيدة" in t for t in titles))
        self.assertTrue(any("نسبة رضا المستفيدين" in t for t in titles))
        self.assertFalse(any("مؤشر محدث" in t for t in titles))
        self.assertTrue(Notification.objects.filter(url=late.get_absolute_url()).exists())
        count = Notification.objects.count()
        call_command("excellence_alerts", stdout=open("/dev/null", "w"))
        self.assertEqual(Notification.objects.count(), count)  # لا تكرار

    def test_pages(self):
        self.client.force_login(self.user)
        initiative = self.initiative(title="برنامج التأهيل", end_date=TODAY - DAY, progress=30)
        kpi = self.kpi()
        KPIReading.objects.create(kpi=kpi, period_end=TODAY, value=76)
        self.plan.approve(self.user)
        html = self.client.get("/excellence/").content.decode()
        self.assertIn("ريادة في التنمية", html)
        self.assertIn("برنامج التأهيل", html)  # في المتأخرة
        self.assertIn("ex-green", html)
        report = self.client.get(f"/excellence/report/?plan={self.plan.pk}")
        self.assertContains(report, "مجتمع حيوي")
        self.assertContains(report, "القضاء على الفقر")
        self.assertEqual(report.context["as_of"], TODAY)
        self.assertEqual(self.client.get("/excellence/kpi-entry/").status_code, 200)
        for slug, obj in (("strategic-plan", self.plan), ("strategic-goal", self.g1), ("initiative", initiative),
                          ("kpi", kpi)):
            for url in (f"/app/{slug}/", f"/app/{slug}/new/", f"/app/{slug}/{obj.pk}/", f"/app/{slug}/{obj.pk}/edit/"):
                self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(f"/app/kpi/{kpi.pk}/"), "سجل القراءات")
        self.assertContains(self.client.get(f"/app/strategic-plan/{self.plan.pk}/"), "تقرير الأداء المؤسسي")

    def test_new_kpi_with_readings_via_form_and_scoped_owner(self):
        self.client.force_login(self.user)
        _, outsider = make_nonprofit("جمعية أخرى", "other")
        form = self.client.get("/app/kpi/new/").content.decode()
        self.assertIn(f'value="{self.user.pk}">tamayuz', form)
        self.assertNotIn(f'value="{outsider.pk}">other', form)
        response = self.client.post("/app/kpi/new/", {
            "goal": self.g1.pk, "name": "نسبة التطوع", "unit": "percent", "target": "50", "direction": "higher",
            "frequency": "annual", "owner": self.user.pk,
            "readings-TOTAL_FORMS": "1", "readings-INITIAL_FORMS": "0", "readings-MIN_NUM_FORMS": "0",
            "readings-MAX_NUM_FORMS": "1000",
            "readings-0-period_end": f"{TODAY:%Y-%m-%d}", "readings-0-value": "45", "readings-0-note": ""})
        self.assertEqual(response.status_code, 302, response.content.decode()[:2000])
        kpi = KPI.objects.get(name="نسبة التطوع")
        self.assertEqual(kpi.achievement, Decimal("90.0"))

    def test_empty_dashboard(self):
        self.client.force_login(self.user)
        StrategicPlan.objects.all().delete()
        self.assertContains(self.client.get("/excellence/"), "ابدأ بالخطة الاستراتيجية")
        self.assertContains(self.client.get("/excellence/report/"), "لا توجد خطة")
