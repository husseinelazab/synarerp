import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from programs.models import (EnrollmentStatus, ProgramEnrollment, ProgramSession, ProgramStatus, SessionAttendance,
                             TrainingProgram)

TODAY = timezone.localdate()


class ProgramsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية تمكين", "tamkeen")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.program = TrainingProgram.objects.create(
            title="مهارات القيادة الشبابية", track="مسار القيادة", trainer="أ. سعد", start_date=TODAY,
            end_date=TODAY + datetime.timedelta(days=10), hours=Decimal("12"), capacity=2)
        self.sessions = [ProgramSession.objects.create(parent=self.program, date=TODAY + datetime.timedelta(days=i),
                                                       topic=f"الجلسة {i + 1}") for i in range(4)]

    def enroll(self, national_id, name="متدرب", status=EnrollmentStatus.ACCEPTED, **extra):
        return ProgramEnrollment.objects.create(program=self.program, full_name=name, national_id=national_id,
                                                phone="0551231234", status=status, **extra)

    def register(self, national_id, name="متدرب", **extra):
        return self.client.post(f"/t/{self.program.public_token}/", {
            "full_name": name, "national_id": national_id, "phone": "0551231234", "gender": "male", **extra})

    def test_end_date_validation(self):
        self.program.end_date = TODAY - datetime.timedelta(days=1)
        with self.assertRaisesMessage(ValidationError, "تاريخ النهاية"):
            self.program.full_clean()

    def test_public_registration_capacity_and_duplicates(self):
        self.assertContains(self.register("1300000001"), "مغلق")  # مسودة: التسجيل لم يُفتح
        self.program.open_registration(self.user)
        self.assertContains(self.client.get(f"/t/{self.program.public_token}/"), "مهارات القيادة الشبابية")
        self.assertContains(self.register("1300000001", "خالد"), "تم استلام طلب تسجيلك")
        self.assertContains(self.register("1300000001", "خالد"), "مسجل في هذا البرنامج مسبقاً")
        self.register("1300000002", website="spam")  # مصيدة البوتات
        self.assertContains(self.register("123"), "10 أرقام")
        self.assertEqual(ProgramEnrollment.objects.count(), 1)
        self.assertEqual(ProgramEnrollment.objects.get().status, EnrollmentStatus.REGISTERED)
        # السعة: مقعدان فقط
        rows = [self.enroll(f"130000001{i}", status=EnrollmentStatus.REGISTERED) for i in range(3)]
        rows[0].accept(self.user)
        rows[1].accept(self.user)
        with self.assertRaisesMessage(ValidationError, "اكتملت المقاعد"):
            rows[2].accept(self.user)
        self.assertContains(self.register("1300000005"), "اكتملت المقاعد")
        self.assertEqual(ProgramEnrollment.objects.count(), 4)
        rows[1].withdraw(self.user)
        rows[2].accept(self.user)
        self.program.start(self.user)
        self.assertContains(self.register("1300000006"), "مغلق")
        self.assertEqual(self.client.get("/t/unknown/").status_code, 404)

    def test_attendance_scores_completion_certificate_and_impact(self):
        self.program.capacity = 5
        self.program.save()
        good = self.enroll("1300000021", "نورة")
        weak = self.enroll("1300000022", "سارة")
        absent = self.enroll("1300000023", "هند")
        pending = self.enroll("1300000024", "ريم", status=EnrollmentStatus.REGISTERED)
        self.client.force_login(self.user)
        url = f"/programs/{self.program.pk}/attendance/"
        page = self.client.get(url)
        self.assertContains(page, "نورة")
        self.assertNotContains(page, "ريم")
        data = {f"att_{e.pk}_{s.pk}": "on" for e in (good, weak) for s in self.sessions}
        data[f"att_{absent.pk}_{self.sessions[0].pk}"] = "on"
        data.update({f"pre_{good.pk}": "40", f"post_{good.pk}": "80", f"sat_{good.pk}": "5",
                     f"pre_{weak.pk}": "40", f"post_{weak.pk}": "50", f"sat_{weak.pk}": "3",
                     f"pre_{absent.pk}": "40", f"post_{absent.pk}": "abc", f"sat_{absent.pk}": "9"})
        self.assertRedirects(self.client.post(url, data), url)
        for e in (good, weak, absent):
            e.refresh_from_db()
        self.assertEqual((good.pre_score, good.post_score, good.satisfaction), (Decimal("40"), Decimal("80"), 5))
        self.assertEqual((absent.post_score, absent.satisfaction), (None, None))
        self.assertEqual(good.attendance_percent, Decimal("100"))
        self.assertEqual(absent.attendance_percent, Decimal("25"))
        self.assertEqual(SessionAttendance.objects.filter(present=True).count(), 9)
        # إعادة الحفظ تُحدّث الصف نفسه
        self.client.post(url, data)
        self.assertEqual(SessionAttendance.objects.count(), 12)

        self.program.complete(self.user)
        for e in (good, weak, absent, pending):
            e.refresh_from_db()
        self.assertEqual(self.program.status, ProgramStatus.COMPLETED)
        self.assertEqual([e.status for e in (good, weak, absent, pending)],
                         [EnrollmentStatus.PASSED, EnrollmentStatus.FAILED, EnrollmentStatus.FAILED,
                          EnrollmentStatus.REGISTERED])
        self.assertTrue(good.certificate_code)
        self.assertFalse(weak.certificate_code)

        impact = self.program.impact()
        self.assertEqual((impact["enrolled"], impact["completed"]), (3, 1))
        self.assertEqual(impact["completion_rate"], Decimal("33.3"))
        self.assertEqual((impact["avg_pre"], impact["avg_post"]), (Decimal("40"), Decimal("65")))
        self.assertEqual(impact["improvement"], Decimal("62.5"))
        self.assertEqual(impact["avg_satisfaction"], Decimal("4"))
        self.assertEqual(impact["trained_hours"], Decimal("12"))

        certificate = self.client.get(f"/programs/certificate/{good.pk}/")
        self.assertContains(certificate, "نورة")
        self.assertContains(certificate, good.certificate_code)
        self.assertEqual(self.client.get(f"/programs/certificate/{weak.pk}/").status_code, 404)
        report = self.client.get("/programs/impact/", {"from_date": TODAY.isoformat(), "to_date": TODAY.isoformat()})
        self.assertContains(report, "مهارات القيادة")
        self.assertEqual(report.context["programs"][0].metrics["improvement"], Decimal("62.5"))
        self.assertEqual(report.context["from_date"], TODAY)
        self.assertNotContains(self.client.get("/programs/impact/", {"from_date": "2000-01-01",
                                                                     "to_date": "2000-12-31"}), "مهارات القيادة")
        dashboard = self.client.get("/programs/")
        self.assertEqual(dashboard.context["hours"], Decimal("12"))
        self.assertEqual(dashboard.context["trainees"], 3)
        self.client.logout()
        self.assertContains(self.client.get(f"/t/verify/{good.certificate_code.lower()}/"), "شهادة صحيحة")
        self.assertContains(self.client.get("/t/verify/NOPE/"), "لا توجد شهادة")

    def test_program_without_sessions_counts_full_attendance(self):
        ProgramSession.objects.filter(parent=self.program).delete()
        e = self.enroll("1300000031", post_score=Decimal("59"))
        self.assertEqual(e.attendance_percent, Decimal("100"))
        self.program.complete(self.user)
        e.refresh_from_db()
        self.assertEqual(e.status, EnrollmentStatus.FAILED)  # الدرجة البعدية دون 60

    def test_fee_collection_creates_draft_receipt(self):
        e = self.enroll("1300000041", "فهد")
        self.assertNotIn("collect_fee", dict(e.available_actions))
        self.program.fee = Decimal("150")
        self.program.save()
        e = ProgramEnrollment.objects.get(pk=e.pk)
        self.assertIn("collect_fee", dict(e.available_actions))
        self.client.force_login(self.user)
        response = self.client.post(f"/app/program-enrollment/{e.pk}/collect_fee/")
        e.refresh_from_db()
        receipt = e.fee_receipt
        self.assertRedirects(response, f"/app/{receipt.doctype_slug}/{receipt.pk}/edit/", fetch_redirect_response=False)
        self.assertTrue(receipt.is_draft)
        self.assertEqual((receipt.amount, receipt.donation_type, receipt.income_account.number),
                         (Decimal("150"), "general", "31205005"))
        self.assertEqual(receipt.account, self.company.default_cash_account)
        self.assertIn("رسوم البرنامج", receipt.remarks)
        self.assertNotIn("collect_fee", dict(e.available_actions))
        with self.assertRaises(ValidationError):
            e.collect_fee(self.user)

    def test_pages(self):
        e = self.enroll("1300000051", "ماجد")
        self.program.open_registration(self.user)
        self.client.force_login(self.user)
        self.register("1300000052")  # المستخدم المسجل دخوله يرى الصفحة العامة أيضاً
        self.assertEqual(ProgramEnrollment.objects.count(), 2)
        html = self.client.get("/programs/").content.decode()
        self.assertIn("لوحة البرامج والتطوير", html)
        self.assertIn("طلبات التسجيل", html)
        for url in ("/programs/impact/", f"/programs/{self.program.pk}/attendance/", "/app/training-program/",
                    "/app/training-program/new/", f"/app/training-program/{self.program.pk}/",
                    f"/app/training-program/{self.program.pk}/edit/", "/app/program-enrollment/",
                    "/app/program-enrollment/new/", f"/app/program-enrollment/{e.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        detail = self.client.get(f"/app/training-program/{self.program.pk}/")
        self.assertContains(detail, self.program.public_token)
        self.assertContains(detail, f"/programs/{self.program.pk}/attendance/")
        self.client.post(f"/app/training-program/{self.program.pk}/start/")
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, ProgramStatus.RUNNING)

    def test_create_program_with_sessions_via_form(self):
        self.client.force_login(self.user)
        response = self.client.post("/app/training-program/new/", {
            "title": "ورشة كتابة المبادرات", "program_type": "workshop", "mode": "remote",
            "start_date": TODAY.isoformat(), "end_date": TODAY.isoformat(), "hours": "3", "capacity": "30",
            "fee": "0", "pass_score": "60", "min_attendance": "75",
            "sessions-TOTAL_FORMS": "2", "sessions-INITIAL_FORMS": "0", "sessions-MIN_NUM_FORMS": "0",
            "sessions-MAX_NUM_FORMS": "1000",
            "sessions-0-date": TODAY.isoformat(), "sessions-0-topic": "المفاهيم",
            "sessions-1-date": TODAY.isoformat(), "sessions-1-topic": "التطبيق"})
        program = TrainingProgram.objects.get(title="ورشة كتابة المبادرات")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(program.sessions.count(), 2)
        self.assertEqual(program.status, ProgramStatus.DRAFT)
