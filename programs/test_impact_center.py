import datetime
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from accounts.models import GLEntry
from beneficiaries.tests import make_nonprofit
from core.models import Membership, Role
from core.tenancy import tenant_context
from programs.models import ImpactGoal, ImpactMeasurement, TrainingProgram, ProgramCostLink, ProgramEnrollment, EnrollmentStatus
from projects.models import Project


class ImpactCenterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_nonprofit("جمعية أثر موثق","impact_owner")
        cls.other,cls.foreign=make_nonprofit("جهة أثر أخرى","impact_foreign")
        cls.worker=User.objects.create_user("impact_worker",password="pass12345")
        Membership.objects.create(company=cls.company,user=cls.worker,role=Role.USER)
        cls.today=timezone.localdate()
        with tenant_context(cls.company):
            cls.program=TrainingProgram.objects.create(title="برنامج نتائج قابلة للقياس",start_date=cls.today,end_date=cls.today,capacity=5)
            cls.goal=ImpactGoal.objects.create(title="رفع المهارة",module="programs",program=cls.program,unit="درجة",baseline=20,target=80,start_date=cls.today, due_date=cls.today,owner=cls.worker)
            cls.project=Project.objects.create(name="مشروع تكلفة البرنامج",cost_budget=1000)
        with tenant_context(cls.other):
            cls.foreign_goal=ImpactGoal.objects.create(title="مؤشر الجهة الأخرى",module="programs",unit="شخص",baseline=0,target=10,start_date=cls.today,due_date=cls.today,owner=cls.foreign)
            cls.foreign_project=Project.objects.create(name="مشروع جهة أخرى")

    def setUp(self):
        self.client.force_login(self.owner)

    def submit(self,value=50,**extra):
        data={"measured_on":str(self.today),"value":value,"source":"كشف تقييم موثق",**extra}
        response=self.client.post(self.goal.get_absolute_url(),data)
        self.assertEqual(response.status_code,302,response.context and response.context["form"].errors)
        return ImpactMeasurement.objects.filter(goal=self.goal).latest("pk")

    def review(self,row,decision="approve",note=""):
        return self.client.post(reverse("impact_review",args=[row.pk]),{"decision":decision,"note":note})

    def test_pending_not_counted_and_approval_is_immutable(self):
        self.client.force_login(self.worker)
        row=self.submit()
        self.assertIsNone(self.client.get(reverse("impact_center")).context["rows"][0]["actual"])
        self.assertEqual(self.review(row).status_code,403)
        self.client.force_login(self.owner)
        self.review(row)
        page=self.client.get(reverse("impact_center"))
        self.assertEqual(page.context["rows"][0]["progress"],Decimal("50.0"))
        self.review(row,"reject","تغيير قرار سابق")
        row.refresh_from_db();self.assertEqual(row.status,"approved")
        self.assertIsNotNone(row.reviewed_at)

    def test_reject_requires_reason_latest_date_wins_and_zero_is_a_result(self):
        row=self.submit(0)
        self.review(row,"reject");row.refresh_from_db();self.assertEqual(row.status,"pending")
        self.review(row,"approve")
        page=self.client.get(reverse("impact_center"))
        self.assertEqual(page.context["rows"][0]["actual"],0)
        newer=self.submit(80);self.review(newer)
        self.assertTrue(self.client.get(reverse("impact_center")).context["rows"][0]["achieved"])

    def test_goal_definitions_freeze_after_measurement_and_date_validation(self):
        self.submit()
        payload={"title":self.goal.title,"module":"programs","program":self.program.pk,"unit":"شخص","baseline":0,"target":999,"direction":"increase","start_date":str(self.today),"due_date":str(self.today),"owner":self.worker.pk,"description":"تعريف محدث"}
        self.assertEqual(self.client.post(reverse("impact_goal_edit",args=[self.goal.pk]),payload).status_code,302)
        self.goal.refresh_from_db();self.assertEqual(self.goal.target,80);self.assertEqual(self.goal.unit,"درجة")
        future=self.today+datetime.timedelta(days=1)
        response=self.client.post(self.goal.get_absolute_url(),{"measured_on":str(future),"value":40,"source":"كشف"})
        self.assertEqual(response.status_code,200);self.assertIn("measured_on",response.context["form"].errors)

    def test_scope_permissions_export_and_evidence(self):
        row=self.submit(evidence_file=SimpleUploadedFile("proof.pdf",b"%PDF-1.4\nproof",content_type="application/pdf"))
        response=self.client.get(reverse("impact_evidence",args=[row.pk]))
        self.assertEqual(response.status_code,200);self.assertIn("attachment",response["Content-Disposition"])
        self.client.force_login(self.foreign)
        self.assertEqual(self.client.get(self.goal.get_absolute_url()).status_code,404)
        self.assertEqual(self.client.get(reverse("impact_evidence",args=[row.pk])).status_code,404)
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("impact_goal_new")).status_code,403)
        Membership.objects.filter(company=self.company,user=self.worker).update(can_export=False)
        self.assertEqual(self.client.get(reverse("impact_center"),{"format":"csv"}).status_code,403)
        Membership.objects.filter(company=self.company,user=self.worker).update(modules=["accounts"])
        self.assertEqual(self.client.get(reverse("impact_center")).status_code,403)
        self.assertNotContains(self.client.get(reverse("work_center")),self.goal.title)

    def test_evidence_rejects_unsafe_files_and_lower_direction(self):
        response=self.client.post(self.goal.get_absolute_url(),{"measured_on":str(self.today),"value":40,"source":"مصدر","evidence_file":SimpleUploadedFile("script.svg",b"<svg/>")})
        self.assertEqual(response.status_code,200);self.assertIn("evidence_file",response.context["form"].errors)
        with tenant_context(self.company):
            self.goal.direction="decrease";self.goal.baseline=100;self.goal.target=20;self.goal.save()
        row=self.submit(60);self.review(row)
        self.assertEqual(self.client.get(reverse("impact_center")).context["rows"][0]["progress"],50)

    def test_costs_use_expense_net_ignore_cancelled_and_hide_without_permission(self):
        with tenant_context(self.company):
            ProgramCostLink.objects.create(program=self.program,project=self.project,linked_by=self.owner)
            account=self.company.default_cogs_account
            for debit,credit,cancelled in [(300,0,False),(0,40,False),(999,0,True)]:
                GLEntry.objects.create(posting_date=self.today,account=account,project=self.project,debit=debit,credit=credit,is_cancelled=cancelled,voucher_type="test",voucher_no="COST",voucher_id=1)
            for i in range(2):
                ProgramEnrollment.objects.create(program=self.program,full_name=f"مشارك {i}",national_id=f"123456789{i}",phone="0500000000",status=EnrollmentStatus.ACCEPTED)
            ProgramEnrollment.objects.create(program=self.program,full_name="طلب معلق",national_id="1234567899",phone="0500000000",status=EnrollmentStatus.REGISTERED)
        row=self.client.get(reverse("impact_center")).context["costs"][0]
        self.assertEqual((row["actual"],row["per_person"],row["variance"]),(260,130,740))
        self.client.force_login(self.worker)
        Membership.objects.filter(company=self.company,user=self.worker).update(hide_costs=True)
        page=self.client.get(reverse("impact_center"))
        self.assertEqual(page.context["costs"],[]);self.assertNotContains(page,self.project.name)

    def test_cost_link_requires_confirmation_and_rejects_foreign_project(self):
        url=reverse("impact_cost_link",args=[self.program.pk])
        response=self.client.post(url,{"project":self.foreign_project.pk,"confirm":"on"})
        self.assertEqual(response.status_code,200);self.assertIn("project",response.context["form"].errors)
        response=self.client.post(url,{"project":self.project.pk})
        self.assertEqual(response.status_code,200);self.assertIn("confirm",response.context["form"].errors)
        self.assertEqual(self.client.post(url,{"project":self.project.pk,"confirm":"on"}).status_code,302)

    def test_paired_scores_prevent_misleading_improvement_and_work_queue(self):
        with tenant_context(self.company):
            for i,pre,post in [(0,20,40),(1,100,None),(2,None,0)]:
                ProgramEnrollment.objects.create(program=self.program,full_name=f"متدرب {i}",national_id=f"123456788{i}",phone="0500000000",status=EnrollmentStatus.ACCEPTED,pre_score=pre,post_score=post)
            result=self.program.impact()
            self.assertEqual(result["improvement"],100);self.assertEqual(result["paired_count"],1)
        self.client.force_login(self.worker)
        self.assertContains(self.client.get(reverse("work_center")),self.goal.title)
        row=self.submit()
        self.client.force_login(self.owner)
        page=self.client.get(reverse("work_center"))
        self.assertTrue(any(r["title"]==self.goal.title for r in page.context["approvals"]))
