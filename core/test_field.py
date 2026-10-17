import json
import uuid
from django.contrib.auth.models import User
from django.test import TestCase
from beneficiaries.tests import make_nonprofit
from beneficiaries.models import Beneficiary, BeneficiaryFollowUp, FieldSubmission
from core.models import Membership, Role
from core.tenancy import tenant_context


class FieldSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_nonprofit("جمعية ميدانية","field_owner")
        cls.worker=User.objects.create_user("field_worker",password="pass12345")
        Membership.objects.create(company=cls.company,user=cls.worker,role=Role.USER)
        with tenant_context(cls.company):
            beneficiary=Beneficiary.objects.create(full_name="أسرة الزيارة",national_id="1234567890",phone="0500000000")
            cls.task=BeneficiaryFollowUp.objects.create(beneficiary=beneficiary,subject="متابعة مسندة",assigned_to=cls.worker)
            BeneficiaryFollowUp.objects.create(beneficiary=beneficiary,subject="مهمة خاصة بالمدير",assigned_to=cls.owner)

    def setUp(self):
        self.client.force_login(self.worker)
        self.payload={"token":str(uuid.uuid4()),"id":self.task.pk,"version":self.task.version,"note":"تمت الزيارة وتوثيق الاحتياجات","completed":True}

    def send(self,payload=None):
        return self.client.post("/work/field/sync/",json.dumps(payload or self.payload),content_type="application/json")

    def test_assigned_only_and_idempotent_retry(self):
        page=self.client.get("/work/field/data/")
        self.assertEqual(len(page.json()["tasks"]),1)
        self.assertNotIn("أسرة الزيارة",page.content.decode())
        self.assertEqual(self.send().status_code,200)
        self.assertTrue(self.send().json()["duplicate"])
        self.task.refresh_from_db();self.assertTrue(self.task.completed)
        self.assertEqual(self.task.notes.count(self.payload["note"]),1)
        self.assertEqual(FieldSubmission.objects.filter(follow_up=self.task).count(),1)

    def test_conflict_and_reassignment_do_not_overwrite(self):
        with tenant_context(self.company):
            self.task.notes="تعديل أحدث من الإدارة";self.task.save()
        self.assertEqual(self.send().status_code,409)
        self.task.refresh_from_db();self.assertEqual(self.task.notes,"تعديل أحدث من الإدارة")
        with tenant_context(self.company):
            self.task.assigned_to=self.owner;self.task.save()
        self.assertEqual(self.send().status_code,403)

    def test_changed_payload_reused_token_and_read_only_are_denied(self):
        self.send()
        changed=dict(self.payload,note="تغيير باستخدام معرّف سابق")
        self.assertEqual(self.send(changed).status_code,409)
        Membership.objects.filter(company=self.company,user=self.worker).update(read_only_doctypes=["beneficiary-follow-up"])
        self.assertEqual(self.send().status_code,403)
        self.assertEqual(self.client.get("/work/field/").status_code,200)

    def test_quran_daily_raster_is_assigned_versioned_and_idempotent(self):
        from programs.models import QuranCircle,QuranStudent,QuranProgress
        from django.utils import timezone
        self.company.association_specialty="quran";self.company.save(update_fields=["association_specialty"])
        with tenant_context(self.company):
            circle=QuranCircle.objects.create(name="حلقة الميدان",teacher="معلم",supervisor=self.worker)
            student=QuranStudent.objects.create(full_name="طالب الرصد",circle=circle)
        response=self.client.get("/work/field/data/")
        task=next(t for t in response.json()["tasks"] if t["kind"]=="quran-progress")
        payload={"kind":"quran-progress","id":student.pk,"day":str(timezone.localdate()),"version":0,"token":str(uuid.uuid4()),"note":"حضر وأنجز الحفظ","completed":False,"attended":True,"pages":"2.50","score":None}
        self.assertEqual(self.send(payload).status_code,200)
        self.assertTrue(self.send(payload).json()["duplicate"])
        with tenant_context(self.company):
            row=QuranProgress.objects.get(student=student)
            self.assertIsNone(row.score);self.assertEqual(str(row.pages_memorized),"2.50")
        payload["token"]=str(uuid.uuid4())
        self.assertEqual(self.send(payload).status_code,409)
        with tenant_context(self.company):
            circle.supervisor=self.owner;circle.save()
        self.assertEqual(self.send(payload).status_code,403)

    def test_trip_and_dawah_notes_sync_without_cross_kind_id_collisions(self):
        from programs.models import NewMuslim,DawahFollowUp,UmrahTrip,UmrahChecklist
        from django.utils import timezone
        self.company.association_specialty="dawah";self.company.save(update_fields=["association_specialty"])
        today=timezone.localdate()
        with tenant_context(self.company):
            person=NewMuslim.objects.create(full_name="مستفيد متابعة",language="العربية")
            follow=DawahFollowUp.objects.create(person=person,title="متابعة تعلم",assigned_to=self.worker,due_date=today)
            trip=UmrahTrip.objects.create(title="رحلة",departure_date=today,return_date=today,supervisor="مشرف",departure_point="المقر")
            task=UmrahChecklist.objects.create(trip=trip,title="تأكيد النقل",assigned_to=self.worker,due_date=today)
        for kind,row in [("dawah-follow-up",follow),("umrah-checklist",task)]:
            payload={"kind":kind,"id":row.pk,"version":row.version,"token":str(uuid.uuid4()),"note":"تم التحقق والإنجاز","completed":True}
            self.assertEqual(self.send(payload).status_code,200)
            row.refresh_from_db();self.assertTrue(row.completed)
