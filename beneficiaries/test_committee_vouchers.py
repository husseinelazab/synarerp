import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from beneficiaries import committee as committee_service
from beneficiaries import vouchers
from beneficiaries.models import (AidCommittee, AidDisbursement, AidProgram, Beneficiary, BeneficiaryCategory,
                                  BeneficiaryStatus, CaseAssessment, CommitteeCase, VoucherMerchant, VoucherRedemption)
from beneficiaries.tests import make_nonprofit
from buying.models import Supplier
from core.models import Membership, Role
from core.tenancy import tenant_context
from funds.models import DonationReceipt

TODAY = timezone.localdate()


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_nonprofit("جمعية اللجنة", "committee_owner")
        cls.members = []
        for n in range(3):
            user = User.objects.create_user(f"member{n}", password="x", first_name=f"عضو {n}")
            Membership.objects.create(user=user, company=cls.company, role=Role.USER)
            cls.members.append(user)
        cls.outsider = User.objects.create_user("not_member", password="x")
        Membership.objects.create(user=cls.outsider, company=cls.company, role=Role.USER)

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.family = Beneficiary.objects.create(full_name="أسرة أبو علي", national_id="1100000077",
                                                 phone="0551112222",
                                                 category=BeneficiaryCategory.objects.get(name="أسر محتاجة"))

    def approve_family(self):
        self.family.status = BeneficiaryStatus.APPROVED
        self.family.next_review_date = TODAY + datetime.timedelta(days=200)
        self.family.save()

    def make_committee(self, **kwargs):
        committee = AidCommittee.objects.create(**{"quorum": 2, **kwargs})
        committee.members.set(self.members)
        return committee

    def assessment(self, score_each=None):
        doc = CaseAssessment.objects.create(beneficiary=self.family)
        doc.calculate_totals()
        doc.save()
        for row in doc.scores.all():
            row.score = row.max_score if score_each is None else min(score_each, row.max_score)
            row.save()
        doc.calculate_totals()
        doc.save()
        return doc

    def donate(self, amount):
        receipt = DonationReceipt(donation_type="general", amount=amount, account=self.company.default_cash_account)
        receipt.full_clean()
        receipt.save()
        receipt.calculate_totals()
        receipt.submit()


class CommitteeTests(Base):
    def test_assessment_needs_committee_decision(self):
        self.make_committee()
        doc = self.assessment()
        self.assertTrue(doc.is_eligible)
        with self.assertRaisesMessage(ValidationError, "عرض على اللجنة"):
            doc.submit()
        case = committee_service.refer(doc, self.owner)
        with self.assertRaisesMessage(ValidationError, "بانتظار قرار"):
            doc.submit()
        with self.assertRaisesMessage(ValidationError, "لأعضاء اللجنة"):
            committee_service.vote(case, self.outsider, "approve")
        with self.assertRaisesMessage(ValidationError, "سبب"):
            committee_service.vote(case, self.members[0], "reject")
        committee_service.vote(case, self.members[0], "reject", "الدخل أعلى من الحد")
        committee_service.vote(case, self.members[1], "reject", "نؤيد")
        case.refresh_from_db()
        self.assertEqual(case.status, "rejected")
        with self.assertRaisesMessage(ValidationError, "صدر قرار"):
            committee_service.vote(case, self.members[2], "approve")
        # قرار اللجنة يحدد الاستحقاق ولو كانت الدرجة مرتفعة
        doc.submit()
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, BeneficiaryStatus.REJECTED)

    def test_majority_tie_waits_and_edit_invalidates(self):
        self.make_committee()
        doc = self.assessment()
        case = committee_service.refer(doc, self.owner)
        committee_service.vote(case, self.members[0], "approve")
        committee_service.vote(case, self.members[1], "reject", "تحفظ")
        case.refresh_from_db()
        self.assertEqual(case.status, "pending")  # تعادل: ينتظر العضو الثالث
        row = doc.scores.first()
        row.score = max(row.max_score - 1, 0)
        row.save()
        with self.assertRaisesMessage(ValidationError, "تغيّرت بيانات"):
            committee_service.vote(case, self.members[2], "approve")
        with self.assertRaisesMessage(ValidationError, "تغيّرت بيانات"):
            doc.submit()
        case = committee_service.refer(doc, self.owner)  # إعادة العرض تبدأ التصويت من جديد
        self.assertEqual(case.votes.count(), 0)
        committee_service.vote(case, self.members[0], "approve")
        committee_service.vote(case, self.members[2], "approve")
        doc.submit()
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, BeneficiaryStatus.APPROVED)

    def test_unanimous_rule_and_disbursement_threshold(self):
        self.approve_family()
        self.donate(10000)
        self.make_committee(decision_rule="unanimous", review_assessments=False, disbursement_threshold=1000)
        program = AidProgram.objects.get(name="مساعدة نقدية")
        small = AidDisbursement.objects.create(beneficiary=self.family, program=program, amount=500)
        small.calculate_totals()
        small.save()
        self.assertIsNone(committee_service.committee_for(small))
        big = AidDisbursement.objects.create(beneficiary=self.family, program=program, amount=3000)
        big.calculate_totals()
        big.save()
        case = committee_service.refer(big, self.owner)
        committee_service.vote(case, self.members[0], "approve")
        committee_service.vote(case, self.members[1], "reject", "نقترح مبلغاً أقل")
        case.refresh_from_db()
        self.assertEqual(case.status, "rejected")  # الإجماع: صوت رفض واحد يكفي
        big.verify_otp(big.issue_otp(), self.owner)
        with self.assertRaisesMessage(ValidationError, "رفضت"):
            big.submit()

    def test_quorum_must_be_reachable(self):
        committee = self.make_committee(quorum=5)
        doc = self.assessment()
        with self.assertRaisesMessage(ValidationError, "أقل من النصاب"):
            committee_service.refer(doc, self.owner)
        committee.is_active = False
        committee.save()
        doc.submit()  # لجنة موقوفة: يُعتمد كما كان

    def test_pages_vote_and_minutes(self):
        self.make_committee()
        doc = self.assessment()
        self.client.force_login(self.owner)
        page = self.client.get(f"/app/case-assessment/{doc.pk}/")
        self.assertContains(page, "عرض على اللجنة")
        self.client.post(f"/beneficiaries/committee/refer/case-assessment/{doc.pk}/")
        case = CommitteeCase.objects.get()
        self.client.force_login(self.members[0])
        page = self.client.get("/beneficiaries/committee/")
        self.assertContains(page, "بانتظار صوتك")
        self.assertContains(page, self.family.full_name)
        self.client.post(f"/beneficiaries/committee/case/{case.pk}/vote/", {"vote": "approve", "next": "committee"})
        self.assertNotContains(self.client.get("/beneficiaries/committee/"), f"committee/case/{case.pk}/vote/")
        minutes = self.client.get(f"/beneficiaries/committee/case/{case.pk}/minutes/")
        self.assertContains(minutes, "عضو 0")
        self.assertContains(minutes, "موافق")


class VoucherTests(Base):
    def setUp(self):
        super().setUp()
        self.approve_family()
        self.donate(10000)
        self.supplier = Supplier.objects.create(name="أسواق التميمي")
        self.merchant = VoucherMerchant.objects.create(supplier=self.supplier)
        self.program = AidProgram.objects.create(
            name="قسائم المواد الغذائية", aid_type="voucher", voucher_valid_days=30,
            expense_account=AidProgram.objects.get(name="مساعدة نقدية").expense_account)

    def issue(self, amount=600):
        aid = AidDisbursement.objects.create(beneficiary=self.family, program=self.program, amount=amount)
        aid.calculate_totals()
        aid.save()
        aid.submit()  # القسيمة لا تحتاج تأكيد استلام برمز (تصل برسالة ويتحقق المتجر من الهوية)
        return aid

    def balances(self, account):
        rows = GLEntry.objects.filter(account=account, is_cancelled=False)
        return sum(r.debit for r in rows), sum(r.credit for r in rows)

    def test_issue_redeem_and_accounting(self):
        aid = self.issue(600)
        voucher = aid.voucher
        self.assertEqual((voucher.amount, voucher.balance, voucher.status), (Decimal("600"), Decimal("600"), "active"))
        self.assertEqual(len(voucher.code), 12)
        liability = vouchers.liability_account(self.company)
        self.assertEqual(self.balances(liability), (0, Decimal("600")))
        with self.assertRaisesMessage(ValidationError, "غير مطابقة"):
            vouchers.find_for_merchant(self.merchant, voucher.code, "9999")
        found = vouchers.find_for_merchant(self.merchant, voucher.code, "0077")
        vouchers.redeem(self.merchant, found, Decimal("250"), "INV-55")
        voucher.refresh_from_db()
        self.assertEqual(voucher.balance, Decimal("350"))
        with self.assertRaisesMessage(ValidationError, "أكبر من رصيد"):
            vouchers.redeem(self.merchant, voucher, Decimal("400"))
        vouchers.redeem(self.merchant, voucher, Decimal("350"))
        voucher.refresh_from_db()
        self.assertEqual((voucher.balance, voucher.status), (0, "used"))
        self.assertEqual(self.balances(liability), (Decimal("600"), Decimal("600")))
        payable = self.company.default_payable_account
        owed = GLEntry.objects.filter(account=payable, supplier=self.supplier, is_cancelled=False)
        self.assertEqual(sum(r.credit for r in owed), Decimal("600"))  # مستحق للمتجر
        with self.assertRaisesMessage(ValidationError, "استُخدمت القسيمة"):
            aid.cancel()

    def test_lock_after_failed_attempts_and_merchant_restriction(self):
        voucher = self.issue().voucher
        for _ in range(5):
            with self.assertRaises(ValidationError):
                vouchers.find_for_merchant(self.merchant, voucher.code, "0000")
        with self.assertRaisesMessage(ValidationError, "مقفلة"):
            vouchers.find_for_merchant(self.merchant, voucher.code, "0077")
        other = VoucherMerchant.objects.create(supplier=Supplier.objects.create(name="متجر آخر"))
        self.program.merchants.set([other])
        voucher.failed_attempts = 0
        voucher.save()
        with self.assertRaisesMessage(ValidationError, "غير معتمد"):
            vouchers.redeem(self.merchant, voucher, Decimal("10"))

    def test_expiry_returns_balance_to_program(self):
        aid = self.issue(500)
        voucher = aid.voucher
        vouchers.redeem(self.merchant, voucher, Decimal("200"))
        expense = self.program.expense_account
        before = self.balances(expense)
        voucher.refresh_from_db()
        voucher.expires_on = TODAY - datetime.timedelta(days=1)
        voucher.save()
        with self.assertRaisesMessage(ValidationError, "انتهت صلاحية"):
            vouchers.redeem(self.merchant, voucher, Decimal("10"))
        call_command("voucher_expiry", stdout=open("/dev/null", "w"))
        voucher.refresh_from_db()
        self.assertEqual((voucher.balance, voucher.status), (0, "expired"))
        after = self.balances(expense)
        self.assertEqual(after[1] - before[1], Decimal("300"))  # الرصيد غير المستخدم يعود للبرنامج
        self.assertEqual(VoucherRedemption.objects.filter(kind="expire").count(), 1)

    def test_cancel_unused_voucher_voids_it(self):
        aid = self.issue(300)
        aid.cancel()
        aid.voucher.refresh_from_db()
        self.assertEqual((aid.voucher.status, aid.voucher.balance), ("void", 0))

    def test_voucher_needs_merchant_and_national_id(self):
        self.merchant.is_active = False
        self.merchant.save()
        aid = AidDisbursement.objects.create(beneficiary=self.family, program=self.program, amount=100)
        aid.calculate_totals()
        aid.save()
        with self.assertRaisesMessage(ValidationError, "متجر شريك"):
            aid.submit()

    @mock.patch("messaging.service.send", return_value=(None, ""))
    def test_merchant_portal(self, send):
        voucher = self.issue(400).voucher
        url = f"/vm/{self.merchant.token}/"
        self.assertEqual(self.client.get("/vm/wrong-token/").status_code, 404)
        page = self.client.get(url)
        self.assertContains(page, "رقم القسيمة")
        checked = self.client.post(url, {"action": "check", "code": voucher.code, "id_last4": "0077"})
        self.assertContains(checked, self.family.full_name)
        self.assertContains(checked, "خصم من القسيمة")
        done = self.client.post(url, {"action": "redeem", "code": voucher.code, "id_last4": "0077", "amount": "150",
                                      "invoice_no": "A-1"})
        self.assertContains(done, "خُصم")
        voucher.refresh_from_db()
        self.assertEqual(voucher.balance, Decimal("250"))
        wrong = self.client.post(url, {"action": "check", "code": voucher.code, "id_last4": "1234"})
        self.assertContains(wrong, "غير مطابقة")
        # الإدارة: القسائم للعرض فقط
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/app/aid-voucher/new/"), "/app/aid-voucher/")
        detail = self.client.get(f"/app/aid-voucher/{voucher.pk}/")
        self.assertContains(detail, "A-1")
        self.assertNotContains(detail, f"/app/aid-voucher/{voucher.pk}/edit/")
        merchant_page = self.client.get(f"/app/voucher-merchant/{self.merchant.pk}/")
        self.assertContains(merchant_page, self.merchant.token)
        old = self.merchant.token
        self.client.post(f"/beneficiaries/merchants/{self.merchant.pk}/regenerate/")
        self.merchant.refresh_from_db()
        self.assertNotEqual(self.merchant.token, old)
        self.assertEqual(self.client.get(url).status_code, 404)
