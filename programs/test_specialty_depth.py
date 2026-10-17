from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from programs.models import QuranCircle,QuranStudent,QuranProgress,QuranLearningPlan,HealthPatient,HealthReferral,NewMuslim,DawahFollowUp


class SpecialtyDepthTests(TestCase):
    @classmethod
    def setUpTestData(cls):cls.company,cls.owner=make_nonprofit("جمعية المسارات المتخصصة","depth_owner")

    def setUp(self):self.client.force_login(self.owner)

    def switch(self,key):
        self.company.association_specialty=key;self.company.save(update_fields=["association_specialty"])

    def test_quran_plan_counts_pages_and_blocks_absent_credit(self):
        self.switch("quran");today=timezone.localdate()
        with tenant_context(self.company):
            circle=QuranCircle.objects.create(name="حلقة",teacher="معلم")
            student=QuranStudent.objects.create(full_name="طالب",circle=circle)
            plan=QuranLearningPlan.objects.create(student=student,title="خطة",start_date=today,end_date=today,target_pages=10)
            QuranProgress.objects.create(student=student,date=today,pages_memorized=2)
            self.assertEqual(plan.completed_pages,2);self.assertEqual(plan.progress_percent,20)
            with self.assertRaises(ValidationError):QuranProgress.objects.create(student=student,date=today,attended=False,pages_memorized=3)

    def test_health_approval_freezes_core_and_dawah_requires_outcome(self):
        self.switch("health")
        with tenant_context(self.company):
            patient=HealthPatient.objects.create(file_number="HEALTH",full_name="مستفيد صحي")
            referral=HealthReferral.objects.create(patient=patient,provider="مركز صحي",service="تنسيق موعد")
        self.client.post(reverse("health_referral_review",args=[referral.pk]),{"decision":"approved"})
        referral.refresh_from_db();self.assertEqual(referral.status,"approved")
        with tenant_context(self.company):
            referral.service="تغيير بعد القرار"
            with self.assertRaises(ValidationError):referral.save()
        self.switch("dawah")
        with tenant_context(self.company):
            person=NewMuslim.objects.create(full_name="مستفيد",language="العربية")
            with self.assertRaises(ValidationError):DawahFollowUp.objects.create(person=person,title="متابعة",assigned_to=self.owner,due_date=timezone.localdate(),completed=True)
