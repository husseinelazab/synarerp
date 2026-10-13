import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from volunteering.models import (OpportunityStatus, ParticipationStatus, Volunteer, VolunteerOpportunity,
                                 VolunteerParticipation)

TODAY = timezone.localdate()


class VolunteeringTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية سواعد", "sawaed")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.opportunity = VolunteerOpportunity.objects.create(
            title="توزيع السلال الرمضانية", start_date=TODAY, end_date=TODAY + datetime.timedelta(days=5),
            required_count=2, expected_hours=6, hour_value=50)

    def register(self, national_id, name="متطوع", gender="male", **extra):
        return self.client.post(f"/v/{self.opportunity.public_token}/", {
            "full_name": name, "national_id": national_id, "phone": "0551231234", "gender": gender, **extra})

    def test_public_registration(self):
        self.assertContains(self.client.get(f"/v/{self.opportunity.public_token}/"), "توزيع السلال الرمضانية")
        self.assertContains(self.register("1300000001", "خالد"), "تم استلام طلبك")
        self.assertContains(self.register("1300000001", "خالد"), "مسجل في هذه الفرصة مسبقاً")
        self.register("1300000002", website="spam")  # مصيدة البوتات
        self.assertEqual(Volunteer.objects.count(), 1)
        self.assertContains(self.register("123"), "10 أرقام")
        self.opportunity.gender = "female"
        self.opportunity.save()
        self.assertContains(self.register("1300000003"), "مخصصة")
        self.opportunity.close_registration(self.user)
        self.assertContains(self.register("1300000004"), "مغلق")
        self.assertEqual(self.client.get("/v/unknown/").status_code, 404)

    def test_participation_cycle_certificate_and_value(self):
        volunteers = [Volunteer.objects.create(full_name=f"متطوع {i}", national_id=f"130000001{i}", phone="1")
                      for i in range(3)]
        rows = [VolunteerParticipation.objects.create(opportunity=self.opportunity, volunteer=v) for v in volunteers]
        rows[0].accept(self.user)
        rows[1].accept(self.user)
        with self.assertRaisesMessage(ValidationError, "اكتمل العدد"):
            rows[2].accept(self.user)
        rows[1].hours = Decimal("8")
        rows[1].save()
        self.opportunity.complete(self.user)
        for row in rows:
            row.refresh_from_db()
        self.assertEqual([r.status for r in rows[:2]], [ParticipationStatus.COMPLETED] * 2)
        self.assertEqual((rows[0].hours, rows[1].hours), (Decimal("6.0"), Decimal("8.0")))
        self.assertEqual(self.opportunity.completed_hours, Decimal("14"))
        self.assertEqual(volunteers[0].total_hours(), Decimal("6"))
        entry = self.opportunity.record_value(self.user)
        lines = {line.account.number: (line.debit, line.credit) for line in entry.accounts.all()}
        self.assertEqual(lines, {"42203001": (Decimal("700"), 0), "31203001": (0, Decimal("700"))})
        entry.submit()
        self.assertNotIn("record_value", dict(self.opportunity.available_actions))
        self.client.force_login(self.user)
        certificate = self.client.get(f"/volunteering/certificate/{rows[0].pk}/")
        self.assertContains(certificate, "متطوع 0")
        self.assertContains(certificate, rows[0].certificate_code)
        self.client.logout()
        self.assertContains(self.client.get(f"/v/verify/{rows[0].certificate_code}/"), "شهادة صحيحة")
        self.assertContains(self.client.get("/v/verify/NOPE/"), "لا توجد شهادة")
        self.assertEqual(self.opportunity.status, OpportunityStatus.COMPLETED)

    def test_pages(self):
        self.client.force_login(self.user)
        self.register("1300000020")  # المستخدم المسجل دخوله يرى الصفحة العامة أيضاً
        self.assertEqual(VolunteerParticipation.objects.count(), 1)
        html = self.client.get("/volunteering/").content.decode()
        self.assertIn("إدارة التطوع", html)
        self.assertIn("طلبات التسجيل", html)
        for url in (f"/app/volunteer-opportunity/{self.opportunity.pk}/", "/app/volunteer-opportunity/new/",
                    f"/app/volunteer/{Volunteer.objects.get().pk}/", "/app/volunteer-participation/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(f"/app/volunteer-opportunity/{self.opportunity.pk}/"),
                            self.opportunity.public_token)
