import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.models import Membership, Role
from core.tenancy import tenant_context
from programs.intake import review_application
from programs.models import (SpecialtyApplication, QuranCircle, QuranStudent, NewMuslim,
                             UmrahTrip, UmrahParticipant, HealthPatient, HealthAppointment)
from website.models import Website
from website.site import clear_cache


class SpecialtyIntakeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_nonprofit("جمعية التحفيظ", "intake_owner")
        cls.other, cls.other_owner = make_nonprofit("جمعية أخرى", "intake_other")
        for company in (cls.company, cls.other):
            company.association_specialty = "quran"
            company.save(update_fields=["association_specialty"])
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="intake", domain="intake.org.sa", is_published=True)
            cls.circle = QuranCircle.objects.create(name="حلقة النور", teacher="أحمد", capacity=1)
        with tenant_context(cls.other):
            cls.other_circle = QuranCircle.objects.create(name="حلقة خاصة أخرى", teacher="سعيد")
        cls.staff = User.objects.create_user("intake_staff", password="pass12345")
        cls.membership = Membership.objects.create(user=cls.staff, company=cls.company, role=Role.USER, modules=["quran"])

    def setUp(self):
        cache.clear()
        clear_cache()
        self.payload = {"full_name": "طالب جديد", "phone": "0551234567", "consent": "on", "circle": self.circle.pk}

    def switch(self, specialty):
        self.company.association_specialty = specialty
        self.company.save(update_fields=["association_specialty"])

    def submit(self, **changes):
        data = dict(self.payload, **changes)
        return self.client.post("/site/intake/register/", data)

    def application(self, **changes):
        data = {"company": self.company, "specialty": self.company.association_specialty,
                "full_name": "طلب اختبار", "phone": "0551234567", "submission_key": "test-key"}
        data.update(changes)
        return SpecialtyApplication.objects.create(**data)

    def test_public_links_domain_and_pending_only(self):
        page = self.client.get("/site/intake/services/")
        self.assertContains(page, "/site/intake/register/")
        self.assertContains(self.client.get("/site/intake/register/"), "حلقة النور")
        self.assertNotContains(self.client.get("/site/intake/register/"), "حلقة خاصة أخرى")
        with override_settings(ALLOWED_HOSTS=["testserver", "intake.org.sa"]):
            self.assertContains(self.client.get("/register/", HTTP_HOST="intake.org.sa"), 'name="circle"')
        with self.captureOnCommitCallbacks(execute=True), patch("website.public_views._notify") as notify:
            self.assertContains(self.submit(status="accepted", company=self.other.pk, specialty="health"), "تم استلام طلبك")
        notify.assert_called_once()
        row = SpecialtyApplication.all_objects.get(company=self.company)
        self.assertEqual((row.status, row.specialty), ("pending", "quran"))
        self.assertFalse(QuranStudent.all_objects.filter(company=self.company).exists())

    def test_idempotent_submission_and_tenant_input_validation(self):
        self.submit()
        self.submit()
        self.assertEqual(SpecialtyApplication.all_objects.count(), 1)
        self.assertNotContains(self.submit(circle=self.other_circle.pk), "تم استلام طلبك")
        self.assertNotContains(self.submit(consent=""), "تم استلام طلبك")
        self.assertNotContains(self.submit(phone="bad"), "تم استلام طلبك")
        self.assertNotContains(self.submit(birth_date="2999-01-01"), "تم استلام طلبك")
        self.assertEqual(SpecialtyApplication.all_objects.count(), 1)

    def test_disabled_unpublished_general_and_closed_plan(self):
        Website.all_objects.filter(pk=self.site.pk).update(allow_specialty_registration=False)
        self.assertEqual(self.client.get("/site/intake/register/").status_code, 404)
        Website.all_objects.filter(pk=self.site.pk).update(allow_specialty_registration=True, is_published=False)
        self.assertEqual(self.client.get("/site/intake/register/").status_code, 404)
        Website.all_objects.filter(pk=self.site.pk).update(is_published=True)
        self.company.subscription.plan.closed_modules = ["quran"]
        self.company.subscription.plan.save(update_fields=["closed_modules"])
        self.assertEqual(self.client.get("/site/intake/register/").status_code, 404)
        self.switch("general")
        self.assertEqual(self.client.get("/site/intake/register/").status_code, 404)

    def test_honeypot_rate_limit_and_csrf(self):
        self.assertContains(self.submit(website="bot"), "تم استلام طلبك")
        self.assertEqual(SpecialtyApplication.all_objects.count(), 0)
        with patch("programs.intake_views.too_many", return_value=True):
            self.assertNotContains(self.submit(), "تم استلام طلبك")
        from django.test import Client
        self.assertEqual(Client(enforce_csrf_checks=True).post("/site/intake/register/", self.payload).status_code, 403)

    def test_quran_review_capacity_and_repeated_decision(self):
        with tenant_context(self.company):
            row = self.application(circle=self.circle)
            accepted = review_application(row, self.owner, {"decision": "accept", "circle": self.circle})
            self.assertEqual(accepted.status, "accepted")
            self.assertEqual(accepted.student.guardian_phone, row.phone)
            self.assertEqual(accepted.reviewed_by_id, self.owner.pk)
            with self.assertRaises(ValidationError):
                review_application(row, self.owner, {"decision": "accept", "circle": self.circle})
            second = self.application(submission_key="second")
            with self.assertRaises(ValidationError):
                review_application(second, self.owner, {"decision": "accept", "circle": self.circle})
            second.refresh_from_db()
            self.assertEqual(second.status, "pending")
            self.assertEqual(QuranStudent.objects.count(), 1)

    def test_reject_requires_reason_and_never_creates_profile(self):
        with tenant_context(self.company):
            row = self.application()
            with self.assertRaises(ValidationError):
                review_application(row, self.owner, {"decision": "reject"})
            result = review_application(row, self.owner, {"decision": "reject", "review_note": "خارج نطاق الخدمة"})
            self.assertEqual(result.status, "rejected")
            self.assertFalse(QuranStudent.objects.exists())

    def test_staff_queue_filters_review_and_readonly_security(self):
        self.submit()
        row = SpecialtyApplication.all_objects.get(company=self.company)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get("/programs/specialty/"), row.full_name)
        self.assertContains(self.client.get(f"/programs/specialty/{row.pk}/review/"), "قرار الموظف")
        self.assertEqual(self.client.get("/programs/specialty/?q=غيرموجود").context["page_obj"].paginator.count, 0)
        self.membership.read_only_doctypes = ["specialty-application"]
        self.membership.save()
        self.assertEqual(self.client.post(f"/programs/specialty/{row.pk}/review/", {"decision": "accept", "circle": self.circle.pk}).status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.membership.read_only_doctypes = ["quran-student"]
        self.membership.save()
        self.assertEqual(self.client.post(f"/programs/specialty/{row.pk}/review/", {"decision": "accept", "circle": self.circle.pk}).status_code, 403)
        self.membership.read_only_doctypes = []
        self.membership.can_submit = False
        self.membership.save()
        self.assertContains(self.client.post(f"/programs/specialty/{row.pk}/review/", {"decision": "accept", "circle": self.circle.pk}), "صلاحيتك إنشاء المسودات")
        self.membership.can_submit = True
        self.membership.save()
        self.assertEqual(self.client.post(f"/programs/specialty/{row.pk}/review/", {"decision": "accept", "circle": self.circle.pk}).status_code, 302)
        self.assertContains(self.client.get(f"/programs/specialty/{row.pk}/review/"), "سجل القرار")
        self.assertContains(self.client.get("/programs/specialty/?status=accepted"), row.full_name)

    def test_other_tenant_and_specialty_cannot_access(self):
        row = self.application()
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(f"/programs/specialty/{row.pk}/review/").status_code, 404)
        self.client.force_login(self.owner)
        self.switch("health")
        self.assertEqual(self.client.get(f"/programs/specialty/{row.pk}/review/").status_code, 404)
        self.assertEqual(self.client.get(f"/app/specialty-application/{row.pk}/").status_code, 404)

    def test_umrah_intake_accept_reuse_and_full_trip_rolls_back(self):
        self.switch("dawah")
        with tenant_context(self.company):
            trip = UmrahTrip.objects.create(title="عمرة شهرية", departure_date=timezone.localdate()+timedelta(days=5),
                return_date=timezone.localdate()+timedelta(days=6), supervisor="مشرف", departure_point="المقر", capacity=1)
        self.assertContains(self.submit(trip=trip.pk, language="العربية", nationality="مصري"), "تم استلام طلبك")
        with tenant_context(self.company):
            row = SpecialtyApplication.objects.get()
            self.assertFalse(NewMuslim.objects.exists())
            accepted = review_application(row, self.owner, {"decision": "accept"})
            self.assertEqual(accepted.participant.trip_id, trip.pk)
            second = self.application(trip=trip, language="الإنجليزية", submission_key="full-trip")
            with self.assertRaises(ValidationError):
                review_application(second, self.owner, {"decision": "accept"})
            self.assertEqual(NewMuslim.objects.count(), 1)
            second.refresh_from_db()
            self.assertEqual(second.status, "pending")
            third = self.application(language="العربية", submission_key="existing")
            review_application(third, self.owner, {"decision": "accept", "existing_profile": accepted.new_muslim})
            self.assertEqual(NewMuslim.objects.count(), 1)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/programs/specialty/"), "الرحلات القادمة والمقاعد المتبقية")

    def test_health_intake_review_schedules_appointment_only_after_approval(self):
        self.switch("health")
        self.assertNotContains(self.submit(), "تم استلام طلبك")
        self.assertContains(self.submit(service_needed="عيادة العيون"), "تم استلام طلبك")
        self.client.force_login(self.owner)
        row = SpecialtyApplication.all_objects.get(company=self.company)
        self.assertFalse(HealthPatient.all_objects.exists())
        url = f"/programs/specialty/{row.pk}/review/"
        self.assertEqual(self.client.post(url, {"decision": "accept", "appointment_at": "2099-01-01T10:00"}).status_code, 200)
        self.assertFalse(HealthPatient.all_objects.exists())
        self.assertEqual(self.client.post(url, {"decision": "accept", "appointment_at": "2099-01-01T10:00", "clinician": "طبيب", "clinic": "عيون"}).status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.patient.service_needed, "عيادة العيون")
        self.assertIsNotNone(row.appointment_id)
        self.assertContains(self.client.get("/programs/specialty/"), "المواعيد القادمة")

    def test_dashboard_overdue_trend_and_hidden_record_metrics(self):
        row = self.application()
        SpecialtyApplication.all_objects.filter(pk=row.pk).update(created_at=timezone.now()-timedelta(days=3))
        self.client.force_login(self.staff)
        page = self.client.get("/programs/specialty/")
        self.assertEqual(page.context["overdue"], 1)
        self.assertEqual(sum(p["total"] for p in page.context["trend"]), 1)
        self.assertContains(page, "إشغال الحلقات")
        self.membership.denied_doctypes = ["quran-student"]
        self.membership.save()
        page = self.client.get("/programs/specialty/")
        self.assertNotContains(page, "الطلاب النشطون")
        self.assertNotContains(page, "إشغال الحلقات")

    def test_existing_student_reuses_profile_and_wrong_tenant_profile_is_rejected(self):
        with tenant_context(self.other):
            outsider = QuranStudent.objects.create(full_name="طالب خارجي", circle=self.other_circle)
        with tenant_context(self.company):
            row = self.application(circle=self.circle)
            with self.assertRaises(ValidationError):
                review_application(row, self.owner, {"decision": "accept", "existing_profile": outsider})
            student = QuranStudent.objects.create(full_name="طالب سابق", circle=self.circle)
            result = review_application(row, self.owner, {"decision": "accept", "existing_profile": student, "circle": self.circle})
            self.assertEqual(result.student_id, student.pk)
            self.assertEqual(QuranStudent.objects.count(), 1)

    def test_closed_trip_rolls_back_profile_and_patient_reuse(self):
        self.switch("dawah")
        with tenant_context(self.company):
            trip = UmrahTrip.objects.create(title="مغلقة", departure_date=timezone.localdate()+timedelta(days=1),
                return_date=timezone.localdate()+timedelta(days=2), supervisor="م", departure_point="المقر", status="closed")
            row = self.application(trip=trip, language="العربية")
            with self.assertRaises(ValidationError):
                review_application(row, self.owner, {"decision": "accept"})
            self.assertFalse(NewMuslim.objects.exists())
        self.switch("health")
        with tenant_context(self.company):
            patient = HealthPatient.objects.create(file_number="P1", full_name="ملف سابق")
            row = self.application(submission_key="patient", service_needed="أسنان")
            review_application(row, self.owner, {"decision": "accept", "existing_profile": patient})
            self.assertEqual(HealthPatient.objects.count(), 1)
            self.assertFalse(HealthAppointment.objects.exists())

    def test_application_generic_crud_cannot_bypass_review(self):
        row = self.application()
        self.client.force_login(self.owner)
        for url in ("/app/specialty-application/new/", f"/app/specialty-application/{row.pk}/edit/"):
            self.client.post(url, {"status": "accepted", "full_name": "modified"})
        self.client.post("/app/specialty-application/bulk/", {"action": "delete", "ids": [row.pk]})
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.full_name, "طلب اختبار")
        self.assertFalse(QuranStudent.all_objects.exists())

    def test_queue_date_filters_and_pagination(self):
        for i in range(27):
            self.application(full_name=f"طالب {i}", submission_key=f"page-{i}")
        self.client.force_login(self.owner)
        first = self.client.get("/programs/specialty/")
        self.assertEqual(len(first.context["page_obj"]), 25)
        self.assertEqual(len(self.client.get("/programs/specialty/?page=2").context["page_obj"]), 2)
        filtered = self.client.get("/programs/specialty/?from_date=2099-01-01")
        self.assertEqual(filtered.context["page_obj"].paginator.count, 0)
        self.assertEqual(filtered.context["counts"]["total"], 27)
        invalid = self.client.get("/programs/specialty/?from_date=2099-01-01&to_date=2020-01-01")
        self.assertContains(invalid, "بداية الفترة لا تتجاوز نهايتها")


    def test_accepted_health_request_can_schedule_later_once(self):
        self.switch("health")
        with tenant_context(self.company):
            row = self.application(service_needed="عيون")
            review_application(row, self.owner, {"decision": "accept"})
        self.client.force_login(self.owner)
        url = f"/programs/specialty/{row.pk}/review/"
        self.assertContains(self.client.get(url), "حجز الموعد وربطه بالطلب")
        payload = {"action": "schedule", "scheduled_at": "2099-01-01T10:00", "clinician": "الطبيب", "clinic": "عيون"}
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        row.refresh_from_db()
        self.assertIsNotNone(row.appointment_id)
        self.assertEqual(self.client.get("/programs/specialty/").context["tasks"][0]["count"], 0)
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.assertEqual(HealthAppointment.all_objects.count(), 1)
        self.assertContains(self.client.get(url), "السجلات المعتمدة المرتبطة بالطلب")


    def test_api_cannot_create_edit_delete_or_review_intake(self):
        import json
        from core.models import ApiKey
        row = self.application()
        with tenant_context(self.company):
            _, token = ApiKey.issue(self.company, self.owner, "اختبار مراجعة الطلبات")
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        detail = f"/api/v1/specialty-application/{row.pk}/"
        page = self.client.get(detail, **headers)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("submission_key", page.json())
        for method, path in [("post", "/api/v1/specialty-application/"), ("patch", detail),
                             ("delete", detail), ("post", detail + "accept/")]:
            response = getattr(self.client, method)(path, data=json.dumps({"status": "accepted"}),
                                                    content_type="application/json", **headers)
            self.assertEqual(response.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")


# PostgreSQL is the production backend: exercise actual row locks with independent connections.
@unittest.skipUnless(connection.vendor == "postgresql", "يتطلب أقفال PostgreSQL الفعلية")
class ConcurrentIntakeReviewTests(TransactionTestCase):
    def _fixture_teardown(self):
        # Flushing the test database is owner maintenance, after the restricted-role assertions.
        from core import rls
        runtime = rls.RUNTIME
        try:
            rls.RUNTIME = False
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
            super()._fixture_teardown()
        finally:
            rls.RUNTIME = runtime
            if runtime:
                rls.activate_role(connection)

    def test_two_reviewers_cannot_overbook_last_circle_seat(self):
        company, owner = make_nonprofit("جمعية مراجعات متزامنة", "concurrent_intake")
        company.association_specialty = "quran"
        company.save(update_fields=["association_specialty"])
        with tenant_context(company):
            circle = QuranCircle.objects.create(name="آخر مقعد", teacher="معلم", capacity=1)
            rows = [SpecialtyApplication.objects.create(specialty="quran", full_name=f"طالب {i}",
                    phone="0551234567", circle=circle, submission_key=f"concurrent-{i}") for i in range(2)]
        barrier = Barrier(2)

        def decide(pk):
            close_old_connections()
            try:
                with tenant_context(company):
                    row = SpecialtyApplication.objects.get(pk=pk)
                    barrier.wait(timeout=20)
                    try:
                        review_application(row, owner, {"decision": "accept", "circle": circle})
                        return "accepted"
                    except ValidationError:
                        return "pending"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, [r.pk for r in rows]))
        self.assertEqual(sorted(results), ["accepted", "pending"])
        with tenant_context(company):
            self.assertEqual(QuranStudent.objects.count(), 1)
            self.assertEqual(SpecialtyApplication.objects.filter(status="accepted").count(), 1)
            self.assertEqual(SpecialtyApplication.objects.filter(status="pending").count(), 1)
