import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from beneficiaries.models import (AssessmentCriterion, Beneficiary, BeneficiaryCategory, BeneficiaryEntity,
                                  BeneficiaryStatus, CaseAssessment, EntityRequest, FamilyMember, RequestStatus)
from core.models import Membership, Role
from core.provisioning import provision_company
from core.tenancy import tenant_context
from saas.models import Plan
from saas.services import ensure_default_plans
from saas.tests import make_company


def make_nonprofit(name, username, role=Role.OWNER):
    ensure_default_plans()
    user = User.objects.create_user(username, f"{username}@example.com", "pass12345")
    company = provision_company(name, plan=Plan.objects.get(code="pro"), organization_type="nonprofit")
    Membership.objects.create(user=user, company=company, role=role)
    return company, user


class NonprofitSetupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية البر", "birr")
        cls.business, cls.business_user = make_company("شركة تجارية", "trader")

    def test_nonprofit_gets_categories_and_criteria_totalling_100(self):
        self.assertTrue(BeneficiaryCategory.all_objects.filter(company=self.company, name="أيتام").exists())
        total = sum(AssessmentCriterion.all_objects.filter(company=self.company).values_list("max_score", flat=True))
        self.assertEqual(total, 100)
        self.assertFalse(BeneficiaryCategory.all_objects.filter(company=self.business).exists())

    def test_switching_business_to_nonprofit_seeds_once(self):
        self.business.organization_type = "nonprofit"
        self.business.save()
        self.business.save()
        self.assertEqual(BeneficiaryCategory.all_objects.filter(company=self.business).count(), 8)

    def test_nonprofit_menu_uses_association_module_names(self):
        self.client.force_login(self.user)
        html = self.client.get("/beneficiaries/").content.decode()
        for label in ("إدارة الشؤون المالية", "إدارة الموارد البشرية", "إدارة التقييم والمتابعة",
                      "إدارة الجهات المستفيدة", "إدارة المخازن والمستودعات", "إدارة التمكين التقني"):
            self.assertIn(label, html)
        for label in ("نقاط البيع", "التصنيع", "إدارة العملاء (CRM)"):
            self.assertNotIn(label, html)
        self.assertIn("فواتير المشتريات", html)  # المشتريات ضمن الشؤون المالية

    def test_business_cannot_see_or_open_nonprofit_modules(self):
        self.client.force_login(self.business_user)
        html = self.client.get("/").content.decode()
        self.assertNotIn("إدارة التقييم والمتابعة", html)
        self.assertIn("المبيعات", html)
        self.assertRedirects(self.client.get("/app/beneficiary/"), "/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/beneficiaries/"), "/", fetch_redirect_response=False)

    def test_signup_as_nonprofit(self):
        response = self.client.post("/signup/", {
            "company_name": "جمعية الإحسان", "organization_type": "nonprofit", "tax_id": "", "phone": "0500000000",
            "full_name": "مدير الجمعية", "email": "ihsan@example.com", "username": "ihsan",
            "password": "Strong-pass-2026", "password2": "Strong-pass-2026",
            "plan": Plan.objects.get(code="pro").pk})
        self.assertEqual(response.status_code, 302)
        company = Membership.objects.get(user__username="ihsan").company
        self.assertTrue(company.is_nonprofit)

    def test_restricted_user_sees_merged_finance_module_with_buying_only(self):
        clerk = User.objects.create_user("clerk", "c@example.com", "pass12345")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER, modules=["buying"])
        self.client.force_login(clerk)
        html = self.client.get("/app/purchase-invoice/").content.decode()
        self.assertIn("إدارة الشؤون المالية", html)
        self.assertNotIn("ميزان المراجعة", html)


class BeneficiaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الخير", "khair")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.family = Beneficiary.objects.create(full_name="فاطمة أحمد علي", national_id="1000000001", phone="0550000001",
                                                 monthly_income=3000, monthly_obligations=600)
        self.family.members.create(full_name="محمد", relation="son", gender="male", monthly_income=0)
        self.family.members.create(full_name="سارة", national_id="1000000002", relation="daughter", gender="female",
                                   monthly_income=600)

    def test_file_number_and_per_capita_income(self):
        self.assertTrue(self.family.file_number.startswith("BEN-"))
        self.assertEqual(self.family.family_size, 3)
        self.assertEqual(self.family.per_capita_income, Decimal("1000"))  # (3000 + 600 − 600) ÷ 3

    def test_national_id_cannot_be_registered_twice(self):
        head = Beneficiary(full_name="آخر", national_id="1000000002", phone="0550000009")
        with self.assertRaises(ValidationError):
            head.full_clean()
        member = FamilyMember(parent=Beneficiary.objects.create(full_name="ب", national_id="1000000003", phone="1"),
                              full_name="ف", national_id="1000000001", relation="other", gender="female")
        with self.assertRaises(ValidationError):
            member.full_clean()
        with self.assertRaises(ValidationError):
            Beneficiary(full_name="خطأ", national_id="3123", phone="1").full_clean()

    def _assessment(self, scores, category=None):
        case = CaseAssessment.objects.create(beneficiary=self.family, researcher=self.user, category=category,
                                             posting_date=datetime.date(2026, 1, 15))
        case.calculate_totals()
        case.save()
        for row, score in zip(case.scores.all(), scores):
            row.score = score
            row.save()
        case.calculate_totals()
        case.save()
        return case

    def test_assessment_approves_and_schedules_review(self):
        orphans = BeneficiaryCategory.objects.get(name="أيتام")
        case = self._assessment([25, 15, 10, 5, 5, 5], category=orphans)  # 65 من 100
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, BeneficiaryStatus.UNDER_STUDY)
        self.assertEqual(case.family_size, 3)
        case.submit()
        self.family.refresh_from_db()
        self.assertTrue(case.is_eligible)
        self.assertEqual(case.score_percent, Decimal("65.0"))
        self.assertEqual(self.family.status, BeneficiaryStatus.APPROVED)
        self.assertEqual(self.family.category, orphans)
        self.assertEqual(self.family.next_review_date, datetime.date(2027, 1, 15))
        case.cancel()
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, BeneficiaryStatus.UNDER_STUDY)
        self.assertIsNone(self.family.next_review_date)

    def test_low_score_marks_not_eligible(self):
        case = self._assessment([10, 5, 5, 5, 5, 5])
        case.submit()
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, BeneficiaryStatus.REJECTED)

    def test_assessment_requires_every_score_within_limit(self):
        case = self._assessment([25])
        with self.assertRaises(ValidationError):
            case.submit()
        row = case.scores.first()
        row.score = row.max_score + 1
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_pages_render(self):
        self.client.force_login(self.user)
        case = self._assessment([25, 15, 10, 5, 5, 5])
        for url in ("/beneficiaries/", "/beneficiaries/entities/", "/app/beneficiary/", "/app/beneficiary/new/",
                    f"/app/beneficiary/{self.family.pk}/", f"/app/case-assessment/{case.pk}/",
                    f"/app/case-assessment/new/?beneficiary={self.family.pk}", "/app/beneficiary-entity/new/",
                    "/app/entity-request/new/", "/settings/company/", "/", "/app/beneficiary/template.xlsx",
                    "/settings/users/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_create_beneficiary_with_family_through_form(self):
        self.client.force_login(self.user)
        response = self.client.post("/app/beneficiary/new/", {
            "full_name": "نورة سالم", "national_id": "1000000010", "nationality": "سعودي", "gender": "female",
            "phone": "0551112222", "registered_on": "2026-02-01", "annual_rent": "0", "monthly_income": "2500",
            "monthly_obligations": "0",
            "members-TOTAL_FORMS": "1", "members-INITIAL_FORMS": "0", "members-MIN_NUM_FORMS": "0",
            "members-MAX_NUM_FORMS": "1000", "members-0-full_name": "خالد", "members-0-relation": "son",
            "members-0-gender": "male", "members-0-monthly_income": "0"})
        self.assertEqual(response.status_code, 302)
        created = Beneficiary.objects.get(national_id="1000000010")
        self.assertEqual(created.family_size, 2)


class EntityRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية التنمية", "tanmia")

    def test_request_workflow(self):
        with tenant_context(self.company):
            entity = BeneficiaryEntity.objects.create(name="مدرسة الحي", entity_type="education")
            request = EntityRequest.objects.create(entity=entity, subject="حقائب مدرسية", requested_amount=5000)
        self.assertTrue(request.number.startswith("EREQ-"))
        self.client.force_login(self.user)
        self.client.post(f"/app/entity-request/{request.pk}/start_review/")
        self.client.post(f"/app/entity-request/{request.pk}/approve/")
        request.refresh_from_db()
        self.assertEqual(request.status, RequestStatus.APPROVED)
        self.assertEqual(request.approved_amount, Decimal("5000"))
        self.assertEqual(request.decided_by, self.user)
        self.client.post(f"/app/entity-request/{request.pk}/complete/")
        request.refresh_from_db()
        self.assertEqual(request.status, RequestStatus.COMPLETED)


class NonprofitDemoTests(TestCase):
    def test_setup_erp_creates_demo_association(self):
        from io import StringIO

        from django.core.management import call_command

        from accounts.models import Fund
        from awqaf.models import WaqfDistribution
        from beneficiaries.models import AidDisbursement
        from core.models import Company
        from funds.reports import activities_data

        call_command("setup_erp", "--nonprofit", "--demo", "--admin", "demo_admin", stdout=StringIO())
        company = Company.objects.get(name="جمعية البر التجريبية")
        self.assertTrue(company.is_nonprofit)
        with tenant_context(company):
            self.assertEqual(Beneficiary.objects.filter(status="approved").count(), 3)
            self.assertEqual(AidDisbursement.objects.filter(docstatus=1).count(), 3)
            self.assertTrue(WaqfDistribution.objects.filter(docstatus=1).exists())
            self.assertEqual(Fund.objects.get(name="صندوق الزكاة").balance, Decimal("27500"))
            today = timezone.localdate()
            data = activities_data(today.replace(month=1, day=1), today)
            self.assertTrue(all(v >= 0 for section in data["sections"] for v in section["totals"]))
        with tenant_context(company):
            from excellence.models import StrategicPlan
            from fleet.models import VehicleRequest
            from programs.models import TrainingProgram
            from relations.models import Partnership

            self.assertEqual(TrainingProgram.objects.get(status="completed").enrollments.filter(status="passed").count(), 2)
            self.assertEqual(StrategicPlan.objects.get().status, "approved")
            self.assertEqual(Partnership.objects.get().status, "active")
            self.assertEqual(VehicleRequest.objects.get().status, "done")
        self.client.force_login(User.objects.get(username="demo_admin"))
        home = self.client.get("/")
        self.assertContains(home, "التبرعات هذا الشهر")
        # القائمة مجمعة تحت عناوين، والوحدات الجديدة ظاهرة للجمعية
        groups = [m["group"] for m in home.context["nav_modules"]]
        self.assertEqual(groups[:2], ["العمل اليومي", "العمل اليومي"])
        for label in ("إدارة البرامج والتطوير", "إدارة التميز المؤسسي", "إدارة العلاقات العامة والإعلام",
                      "إدارة الحركة والصيانة", "إدارة التقارير والإحصائيات"):
            self.assertContains(home, label)
        for url in ("/programs/", "/programs/impact/", "/excellence/", "/excellence/report/", "/relations/",
                    "/relations/report/", "/fleet/", "/fleet/report/", "/statistics/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        stats = self.client.get("/statistics/")
        self.assertEqual(stats.context["stats"]["beneficiaries_approved"], 3)
        self.assertContains(stats, "تقرير الأداء المؤسسي")  # روابط تقارير كل الوحدات
        self.assertContains(self.client.get("/reports/accounts/trial-balance/"), 'class="print-tools"')
