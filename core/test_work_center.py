from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from beneficiaries.tests import make_nonprofit
from beneficiaries.models import Beneficiary, BeneficiaryFollowUp, FamilyMember, DependentCategory
from core.models import Membership, Role, ProcessTemplate, ProcessStage, ProcessCase, ProcessEvent, Attachment, Notification
from core.tenancy import tenant_context
from office.models import Correspondence
from programs.models import HealthPatient


class WorkCenterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_nonprofit("جمعية مركز العمل","work_owner")
        cls.other,cls.foreign=make_nonprofit("جهة أخرى للعمل","work_foreign")
        cls.worker=User.objects.create_user("work_worker",password="pass12345")
        Membership.objects.create(company=cls.company,user=cls.worker,role=Role.USER)
        with tenant_context(cls.company):
            cls.beneficiary=Beneficiary.objects.create(full_name="ملف الأسرة الموحد",national_id="1234567890",phone="0500000000")
            cls.template=ProcessTemplate.objects.create(name="طلب خدمة اجتماعية",module="beneficiaries",enabled=True)
            ProcessStage.objects.create(template=cls.template,position=1,name="دراسة الموظف",assignee=cls.worker,days=2)
            ProcessStage.objects.create(template=cls.template,position=2,name="موافقة المدير",role="manager",days=1,min_amount=100)
            cls.follow=BeneficiaryFollowUp.objects.create(beneficiary=cls.beneficiary,subject="متابعة الأسرة العاجلة",assigned_to=cls.worker)
            cls.letter=Correspondence.objects.create(direction="incoming",subject="معاملة مسندة للموظف",assigned_to=cls.worker)
        with tenant_context(cls.other):
            cls.foreign_beneficiary=Beneficiary.objects.create(full_name="أسرة الجهة الأخرى",national_id="1234567891",phone="0500000001")
            cls.foreign_template=ProcessTemplate.objects.create(name="إجراء جهة أخرى",module="beneficiaries")

    def setUp(self):
        self.client.force_login(self.owner)

    def start(self,amount=150):
        response=self.client.post(reverse("process_start",args=[self.template.pk]),{"title":"طلب دعم للأسرة","description":"تفاصيل الطلب","amount":amount,"beneficiary":self.beneficiary.pk})
        self.assertEqual(response.status_code,302,response.content)
        with tenant_context(self.company):
            return ProcessCase.objects.latest("pk")

    def act(self,case,action="approve",version=None,note=""):
        return self.client.post(reverse("process_act",args=[case.pk]),{"action":action,"version":version or case.version,"note":note})

    def test_snapshot_conditions_sequential_approval_and_duplicate_click(self):
        case=self.start()
        self.assertEqual(len(case.snapshot),2)
        with tenant_context(self.company):
            ProcessStage.objects.filter(template=self.template).update(name="قالب معدل")
        self.assertEqual(case.snapshot[0]["name"],"دراسة الموظف")
        self.assertEqual(self.act(case).status_code,403)  # Manager cannot bypass an explicitly assigned employee.
        self.client.force_login(self.worker)
        self.assertContains(self.client.get(reverse("work_center")),case.title)
        self.assertEqual(self.act(case).status_code,302)
        self.assertEqual(self.act(case).status_code,302)  # stale version, no second approval
        case.refresh_from_db();self.assertEqual(case.current_step,1)
        self.assertEqual(self.act(case).status_code,403)
        self.client.force_login(self.owner)
        self.act(case);case.refresh_from_db()
        self.assertEqual(case.status,"done")
        self.assertEqual(case.events.filter(action="approve").count(),2)
        self.assertIsNone(case.due_date)

    def test_threshold_skip_rejection_requires_reason_and_closed_request_is_immutable(self):
        case=self.start(50)
        self.assertEqual(len(case.snapshot),1)
        self.client.force_login(self.worker)
        self.act(case,"reject");case.refresh_from_db();self.assertEqual(case.status,"active")
        self.act(case,"reject",note="مستندات ناقصة");case.refresh_from_db();self.assertEqual(case.status,"rejected")
        before=case.events.count();self.act(case);self.assertEqual(case.events.count(),before)

    def test_foreign_templates_people_cases_and_module_restrictions(self):
        self.assertEqual(self.client.get(reverse("process_start",args=[self.foreign_template.pk])).status_code,404)
        response=self.client.post(reverse("process_start",args=[self.template.pk]),{"title":"ربط خاطئ","amount":0,"beneficiary":self.foreign_beneficiary.pk})
        self.assertEqual(response.status_code,200)
        self.assertIn("beneficiary",response.context["form"].errors)
        case=self.start()
        self.client.force_login(self.foreign)
        self.assertEqual(self.client.get(case.get_absolute_url()).status_code,404)
        self.assertEqual(self.act(case).status_code,404)
        self.client.force_login(self.worker)
        Membership.objects.filter(company=self.company,user=self.worker).update(modules=["accounts"])
        self.assertNotContains(self.client.get(reverse("work_center")),case.title)
        self.assertEqual(self.client.get(case.get_absolute_url()).status_code,404)

    def test_center_only_assigned_tasks_and_respects_document_denials(self):
        self.client.force_login(self.worker)
        page=self.client.get(reverse("work_center"))
        self.assertContains(page,self.follow.subject);self.assertContains(page,self.letter.subject)
        self.assertNotContains(page,"أسرة الجهة الأخرى")
        Membership.objects.filter(company=self.company,user=self.worker).update(denied_doctypes=["correspondence"])
        self.assertNotContains(self.client.get(reverse("work_center")),self.letter.subject)
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("work_center")),self.follow.subject)

    def test_builder_manager_only_and_validates_cross_company_assignee(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("process_builder_new")).status_code,403)
        self.client.force_login(self.owner)
        payload={"name":"إجراء جديد","module":"beneficiaries","enabled":"on","stages-TOTAL_FORMS":1,"stages-INITIAL_FORMS":0,"stages-MIN_NUM_FORMS":1,"stages-MAX_NUM_FORMS":30,"stages-0-position":1,"stages-0-name":"المراجعة","stages-0-assignee":self.foreign.pk,"stages-0-role":"","stages-0-days":2,"stages-0-min_amount":0}
        self.assertEqual(self.client.post(reverse("process_builder_new"),payload).status_code,200)
        payload["stages-0-assignee"]=self.worker.pk
        self.assertEqual(self.client.post(reverse("process_builder_new"),payload).status_code,302)

    def test_submit_permission_cannot_be_bypassed_by_process(self):
        case=self.start()
        Membership.objects.filter(company=self.company,user=self.worker).update(can_submit=False)
        self.client.force_login(self.worker)
        self.assertEqual(self.act(case).status_code,403)
        case.refresh_from_db();self.assertEqual(case.current_step,0)

    def test_unified_family_followups_and_section_permissions(self):
        with tenant_context(self.company):
            child=FamilyMember.objects.create(parent=self.beneficiary,full_name="تابع مصنف",relation="son",gender="male")
            child.categories.add(DependentCategory.objects.get(name="طالب"))
            Attachment.objects.create(doctype="beneficiary",object_id=self.beneficiary.pk,file_name="family-proof.pdf",content=b"test")
        page=self.client.get(reverse("beneficiary_unified",args=[self.beneficiary.pk]))
        for value in [self.beneficiary.full_name,"تابع مصنف","طالب",self.follow.subject,"family-proof.pdf"]:
            self.assertContains(page,value)
        self.client.force_login(self.worker)
        Membership.objects.filter(company=self.company,user=self.worker).update(denied_doctypes=["beneficiary-follow-up","program-enrollment"])
        page=self.client.get(reverse("beneficiary_unified",args=[self.beneficiary.pk]))
        self.assertNotContains(page,self.follow.subject);self.assertNotContains(page,"البرامج والتدريب")
        self.assertEqual(self.client.get(reverse("beneficiary_unified",args=[self.foreign_beneficiary.pk])).status_code,404)

    def test_specialty_link_explicit_and_cross_company_rejected(self):
        self.company.association_specialty="health";self.company.save()
        with tenant_context(self.company):
            linked=HealthPatient.objects.create(file_number="H-LINK",full_name="مريض مرتبط صراحة",beneficiary=self.beneficiary)
            HealthPatient.objects.create(file_number="H-OTHER",full_name=self.beneficiary.full_name)
            with self.assertRaises(ValidationError):
                HealthPatient.objects.create(file_number="H-FOREIGN",full_name="خطأ",beneficiary=self.foreign_beneficiary)
        page=self.client.get(reverse("beneficiary_unified",args=[self.beneficiary.pk]))
        self.assertContains(page,linked.full_name);self.assertNotContains(page,"H-OTHER")

    def test_document_approval_queue_tracks_next_level_and_permissions(self):
        from core.models import ApprovalRule, ApprovalRecord
        from office.models import AdminRequest, AdminRequestType
        with tenant_context(self.company):
            ApprovalRule.objects.filter(doctype="admin-request").delete()
            kind=AdminRequestType.objects.create(name="طلب إداري للاعتماد")
            doc=AdminRequest.objects.create(request_type=kind,subject="طلب اعتماد داخل مركز العمل",amount=50)
            ApprovalRule.objects.create(doctype="admin-request",level=1,approver_role="user",approver=self.worker)
            ApprovalRule.objects.create(doctype="admin-request",level=2,approver_role="owner")
        self.assertNotContains(self.client.get(reverse("work_center")),doc.subject)
        self.client.force_login(self.worker)
        page=self.client.get(reverse("work_center"))
        self.assertContains(page,doc.subject)
        self.assertEqual(len(page.context["approvals"]),1)
        with tenant_context(self.company):
            ApprovalRecord.objects.create(doctype="admin-request",object_id=doc.pk,level=1,user=self.worker)
        self.assertNotContains(self.client.get(reverse("work_center")),doc.subject)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("work_center")),doc.subject)

    def test_process_list_search_is_tenant_and_participant_scoped(self):
        case=self.start()
        self.assertContains(self.client.get(reverse("process_list"),{"q":"دعم","status":"active"}),case.title)
        self.assertNotContains(self.client.get(reverse("process_list"),{"status":"done"}),case.title)
        self.client.force_login(self.foreign)
        self.assertNotContains(self.client.get(reverse("process_list"),{"q":case.pk}),case.title)
