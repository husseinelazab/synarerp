import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from beneficiaries.tests import make_nonprofit
from core.models import Membership, Role
from core.tenancy import tenant_context
from governance.models import (BoardTerm, FeeStatus, GovernanceAssessment, GovernanceEvidence, GovernanceIndicator,
                               Meeting, MeetingDecision, Member, MembershipCategory, MembershipFee, generate_fees)
from saas.tests import make_company

TODAY = timezone.localdate()
YEAR = TODAY.year


class MembersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية التنمية الأهلية", "tanmia_gov")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.working = MembershipCategory.objects.get(name="عضو عامل")
        self.working.annual_fee = 200
        self.working.save()
        self.members = [Member.objects.create(full_name=f"عضو {i}", phone=f"05500000{i}", category=self.working)
                        for i in range(1, 5)]

    def pay(self, fee, amount=None):
        receipt = fee.collect(self.user)
        if amount:
            receipt.amount = amount
            receipt.save()
        receipt.calculate_totals()
        receipt.submit()
        fee.refresh_from_db()
        return receipt

    def test_fees_collect_to_subscriptions_income_and_voting_rights(self):
        self.assertTrue(self.members[0].member_number.startswith("MEM-"))
        self.assertEqual(generate_fees(YEAR), 4)
        self.assertEqual(generate_fees(YEAR), 0)
        fee = self.members[0].fees.get()
        self.assertFalse(self.members[0].can_vote(YEAR))
        receipt = self.pay(fee)
        self.assertEqual(fee.status, FeeStatus.PAID)
        self.assertTrue(self.members[0].can_vote(YEAR))
        income = GLEntry.objects.get(voucher_type="donation-receipt", voucher_id=receipt.pk, credit__gt=0)
        self.assertEqual(income.account.number, "31205001")
        receipt.cancel()
        fee.refresh_from_db()
        self.assertEqual(fee.status, FeeStatus.PENDING)
        honorary = Member.objects.create(full_name="شرفي", category=MembershipCategory.objects.get(name="عضو شرفي"))
        self.assertFalse(honorary.can_vote(YEAR))  # بلا حق تصويت

    def meeting(self, meeting_type, attended):
        meeting = Meeting.objects.create(meeting_type=meeting_type, title="اجتماع", posting_date=TODAY,
                                         minutes="تمت الموافقة على التقرير السنوي.")
        meeting.calculate_totals()
        meeting.save()
        for row in meeting.attendance.all()[:attended]:
            row.present = True
            row.save()
        meeting.calculate_totals()
        meeting.save()
        return meeting

    def test_general_assembly_quorum_uses_voting_members(self):
        generate_fees(YEAR)
        for member in self.members[:3]:
            self.pay(member.fees.get())
        meeting = self.meeting("ga_ordinary", 1)
        self.assertEqual(meeting.eligible_count, 3)  # الرابع لم يسدد
        with self.assertRaisesMessage(ValidationError, "لم يكتمل النصاب"):
            meeting.submit()
        meeting = self.meeting("ga_ordinary", 2)
        meeting.submit()
        self.assertTrue(meeting.quorum_met)

    def test_board_term_and_meeting(self):
        with self.assertRaisesMessage(ValidationError, "دورة مجلس"):
            self.meeting("board", 0).submit()
        term = BoardTerm.objects.create(name="الدورة الأولى", start_date=TODAY - datetime.timedelta(days=30),
                                        end_date=TODAY + datetime.timedelta(days=1400))
        for member, position in zip(self.members[:3], ("chair", "treasurer", "member")):
            term.seats.create(member=member, position=position)
        overlap = BoardTerm(name="متداخلة", start_date=TODAY, end_date=TODAY + datetime.timedelta(days=10))
        with self.assertRaises(ValidationError):
            overlap.full_clean()
        meeting = self.meeting("board", 2)
        self.assertEqual(meeting.eligible_count, 3)
        meeting.submit()
        MeetingDecision.objects.create(meeting=meeting, text="اعتماد خطة العام", due_date=TODAY - datetime.timedelta(days=1))
        self.assertTrue(MeetingDecision.objects.get().is_overdue)

    def test_pages(self):
        self.client.force_login(self.user)
        meeting = self.meeting("committee", 0)
        html = self.client.get("/").content.decode()
        self.assertIn("إدارة الأعضاء المشاركين", html)
        self.assertIn("إدارة الحوكمة", html)
        for url in ("/governance/members/", "/governance/", f"/app/meeting/{meeting.pk}/", "/app/meeting/new/",
                    f"/app/member/{self.members[0].pk}/", "/app/board-term/new/", "/app/membership-fee/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.client.post("/governance/members/generate-fees/")
        self.assertEqual(MembershipFee.objects.count(), 4)


class GovernanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الحوكمة", "hawkama")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_seeded_standards_and_auto_assessment(self):
        self.assertEqual(GovernanceIndicator.objects.count(), 6)
        manual = GovernanceIndicator.objects.create(standard=GovernanceIndicator.objects.first().standard,
                                                    code="2.1", text="نشر التقرير السنوي على الموقع", weight=2)
        assessment = GovernanceAssessment.objects.create(year=YEAR)
        assessment.calculate_totals()
        assessment.save()
        rows = {r.indicator.code: r for r in assessment.scores.all()}
        self.assertEqual(rows["1.A1"].status, "not_met")  # لا جمعية عمومية بعد
        self.assertIn("0", rows["1.A1"].auto_result)
        self.assertEqual(rows["3.A1"].status, "met")  # لا عجز في الصناديق
        self.assertEqual(rows["1.A4"].status, "met")
        with self.assertRaisesMessage(ValidationError, "2.1"):
            assessment.submit()
        rows["2.1"].status = "partial"
        rows["2.1"].save()
        assessment.calculate_totals()
        assessment.save()
        assessment.submit()
        met = sum(1 for r in assessment.scores.all() if r.status == "met")
        expected = (Decimal(met) + Decimal(1)) * 100 / Decimal(8)  # أوزان: 6 آلية × 1 + 2 يدوي
        self.assertEqual(assessment.score_percent, expected.quantize(Decimal("0.1")))
        self.assertEqual(len(assessment.by_standard()), 3)
        self.assertEqual(manual.evidence.count(), 0)

    def test_evidence_upload_download_and_isolation(self):
        indicator = GovernanceIndicator.objects.get(code="1.A1")
        self.client.force_login(self.user)
        upload = SimpleUploadedFile("محضر.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        self.client.post(f"/governance/indicator/{indicator.pk}/evidence/", {"title": "محضر الجمعية", "file": upload,
                                                                           "year": YEAR})
        evidence = GovernanceEvidence.objects.get()
        response = self.client.get(f"/governance/evidence/{evidence.pk}/download/")
        self.assertEqual(response.content, b"%PDF-1.4 test")
        self.assertIn("attachment", response["Content-Disposition"])
        big = SimpleUploadedFile("big.pdf", b"0" * (5 * 1024 * 1024 + 1))
        self.client.post(f"/governance/indicator/{indicator.pk}/evidence/", {"title": "كبير", "file": big})
        self.assertEqual(GovernanceEvidence.objects.count(), 1)
        self.assertContains(self.client.get(f"/app/governance-indicator/{indicator.pk}/"), "محضر الجمعية")
        other, other_user = make_nonprofit("جمعية أخرى", "other_gov")
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(f"/governance/evidence/{evidence.pk}/download/").status_code, 404)
        viewer = User.objects.create_user("viewer_gov", "v@example.com", "pass12345")
        Membership.objects.create(user=viewer, company=self.company, role=Role.USER, can_delete=False)
        self.client.force_login(viewer)
        self.client.post(f"/governance/evidence/{evidence.pk}/delete/")
        self.assertTrue(GovernanceEvidence.objects.filter(pk=evidence.pk).exists())

    def test_business_cannot_open(self):
        _, user = make_company("شركة عادية", "biz_gov")
        self.client.force_login(user)
        self.assertRedirects(self.client.get("/governance/"), "/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/governance/members/"), "/", fetch_redirect_response=False)
