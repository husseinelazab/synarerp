import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from beneficiaries.models import AidDisbursement, AidProgram, Beneficiary, BeneficiaryCategory, BeneficiaryStatus
from beneficiaries.tests import make_nonprofit
from core.models import Membership, Role
from core.tenancy import tenant_context
from funds.models import DonationReceipt
from stock.models import UOM, Bin, Item, ItemGroup, StockEntry, Warehouse

TODAY = timezone.localdate()


class AidTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الإحسان الخيرية", "ihsan_aid")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.family = Beneficiary.objects.create(full_name="أم محمد", national_id="1100000001", phone="0551234567",
                                                 category=BeneficiaryCategory.objects.get(name="أسر محتاجة"))
        self.approve(self.family)
        self.cash_program = AidProgram.objects.get(name="مساعدة نقدية")
        self.zakat_program = AidProgram.objects.get(name="صرف الزكاة")
        self.basket = AidProgram.objects.get(name="السلة الغذائية")
        self.sent = []
        patcher = mock.patch("beneficiaries.views.send_otp_message",
                             side_effect=lambda company, phone, body, ref, user: self.sent.append(body))
        patcher.start()
        self.addCleanup(patcher.stop)

    def approve(self, beneficiary):
        beneficiary.status = BeneficiaryStatus.APPROVED
        beneficiary.next_review_date = TODAY + datetime.timedelta(days=200)
        beneficiary.save()

    def donate(self, amount, donation_type="general"):
        receipt = DonationReceipt(donation_type=donation_type, amount=amount, account=self.company.default_cash_account)
        receipt.full_clean()
        receipt.save()
        receipt.calculate_totals()
        receipt.submit()

    def aid(self, program, amount=0, confirm=True, **kwargs):
        doc = AidDisbursement.objects.create(beneficiary=kwargs.pop("beneficiary", self.family), program=program,
                                             amount=amount, **kwargs)
        doc.calculate_totals()
        doc.save()
        if confirm:
            doc.verify_otp(doc.issue_otp(), self.user)
        return doc

    def test_default_programs_use_official_accounts(self):
        self.assertEqual(self.cash_program.expense_account.number, "42102002")
        self.assertEqual((self.zakat_program.expense_account.number, self.zakat_program.fund.name),
                         ("42101001", "صندوق الزكاة"))
        self.assertEqual((self.basket.aid_type, self.basket.expense_account.number), ("in_kind", "42103003"))

    def test_cash_aid_requires_receipt_and_posts_to_program(self):
        self.donate(5000)
        doc = self.aid(self.cash_program, 800, confirm=False)
        self.assertEqual(doc.payment_account, self.company.default_cash_account)
        with self.assertRaisesMessage(ValidationError, "رمز التحقق"):
            doc.submit()
        code = doc.issue_otp()
        with self.assertRaisesMessage(ValidationError, "غير صحيح"):
            doc.verify_otp("000000" if code != "000000" else "111111", self.user)
        doc.verify_otp(code, self.user)
        doc.submit()
        lines = {g.account.number: (g.debit, g.credit) for g in
                 GLEntry.objects.filter(voucher_type="aid-disbursement", voucher_id=doc.pk)}
        self.assertEqual(lines, {"42102002": (Decimal("800"), 0), "11103001": (0, Decimal("800"))})

    def test_zakat_aid_limited_by_zakat_fund(self):
        doc = self.aid(self.zakat_program, 500)
        with self.assertRaisesMessage(ValidationError, "لا يكفي"):
            doc.submit()
        self.donate(1000, "zakat")
        doc.refresh_from_db()
        doc.submit()
        self.assertEqual(doc.fund.balance, Decimal("500"))

    def test_eligibility_rules(self):
        other = Beneficiary.objects.create(full_name="غير معتمد", national_id="1100000002", phone="0550000002")
        with self.assertRaisesMessage(ValidationError, "المعتمدين فقط"):
            self.aid(self.cash_program, 100, beneficiary=other).submit()
        self.family.next_review_date = TODAY - datetime.timedelta(days=1)
        self.family.save()
        with self.assertRaisesMessage(ValidationError, "إعادة دراسة"):
            self.aid(self.cash_program, 100).submit()
        self.approve(self.family)
        self.cash_program.categories.add(BeneficiaryCategory.objects.get(name="أيتام"))
        with self.assertRaisesMessage(ValidationError, "ليست من فئات"):
            self.aid(self.cash_program, 100).submit()
        self.cash_program.categories.clear()
        self.cash_program.max_per_year = 1
        self.cash_program.save()
        self.donate(1000)
        self.aid(self.cash_program, 100).submit()
        with self.assertRaisesMessage(ValidationError, "الحد السنوي"):
            self.aid(self.cash_program, 100).submit()

    def test_changes_after_confirmation_require_new_confirmation(self):
        self.donate(1000)
        doc = self.aid(self.cash_program, 100)
        doc.amount = 900
        doc.save()
        with self.assertRaisesMessage(ValidationError, "بعد تأكيد الاستلام"):
            doc.submit()

    def test_otp_expiry_and_attempts(self):
        doc = self.aid(self.cash_program, 100, confirm=False)
        code = doc.issue_otp()
        for _ in range(5):
            with self.assertRaises(ValidationError):
                doc.verify_otp("x", self.user)
        with self.assertRaisesMessage(ValidationError, "عدد المحاولات"):
            doc.verify_otp(code, self.user)
        code = doc.issue_otp()
        AidDisbursement.objects.filter(pk=doc.pk).update(otp_sent_at=doc.otp_sent_at - datetime.timedelta(minutes=11))
        doc.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "انتهت صلاحية"):
            doc.verify_otp(code, self.user)

    def test_in_kind_aid_issues_stock_at_cost(self):
        warehouse = Warehouse.objects.get(name="المستودع الرئيسي")
        rice = Item.objects.create(code="RICE", name="أرز 10 كجم", item_group=ItemGroup.objects.first(),
                                   uom=UOM.objects.first(), is_stock_item=True)
        receipt = StockEntry.objects.create(purpose="receipt", to_warehouse=warehouse)
        receipt.items.create(item=rice, qty=10, rate=50)
        receipt.calculate_totals()
        receipt.submit()
        doc = AidDisbursement.objects.create(beneficiary=self.family, program=self.basket, warehouse=warehouse)
        doc.items.create(item=rice, qty=3)
        doc.calculate_totals()
        doc.save()
        doc.verify_otp(doc.issue_otp(), self.user)
        doc.submit()
        self.assertEqual(doc.amount, Decimal("150"))
        self.assertEqual(Bin.objects.get(item=rice, warehouse=warehouse).actual_qty, Decimal("7"))
        lines = {g.account.number: (g.debit, g.credit) for g in
                 GLEntry.objects.filter(voucher_type="aid-disbursement", voucher_id=doc.pk)}
        self.assertEqual(lines, {"42103003": (Decimal("150"), 0), "11702001": (0, Decimal("150"))})
        doc.cancel()
        self.assertEqual(Bin.objects.get(item=rice, warehouse=warehouse).actual_qty, Decimal("10"))

    def test_without_otp_requirement(self):
        self.company.require_aid_otp = False
        self.company.save()
        self.donate(500)
        doc = self.aid(self.cash_program, 200, confirm=False)
        doc.company.refresh_from_db()
        doc.submit()

    def test_otp_pages_and_manual_confirmation(self):
        self.donate(1000)
        doc = self.aid(self.cash_program, 300, confirm=False)
        clerk = User.objects.create_user("clerk_aid", "c@example.com", "pass12345")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER)
        self.client.force_login(clerk)
        self.assertContains(self.client.get(doc.get_absolute_url()), "إرسال رمز التحقق")
        self.client.post(f"/beneficiaries/aid/{doc.pk}/send-otp/")
        code = self.sent[-1].split(": ")[1][:6]
        self.client.post(f"/beneficiaries/aid/{doc.pk}/confirm/", {"manual": "1", "note": "بلا جوال"})
        doc.refresh_from_db()
        self.assertIsNone(doc.received_at)  # التأكيد اليدوي للمدير فقط
        self.client.post(f"/beneficiaries/aid/{doc.pk}/confirm/", {"code": code})
        doc.refresh_from_db()
        self.assertEqual(doc.receipt_method, "otp")
        self.assertNotContains(self.client.get(doc.get_absolute_url()), "otp")
        self.client.force_login(self.user)
        self.assertNotIn("otp_hash", self.client.get(f"/app/aid-disbursement/{doc.pk}/").content.decode())
        other = self.aid(self.cash_program, 100, confirm=False)
        self.client.force_login(self.user)
        self.client.post(f"/beneficiaries/aid/{other.pk}/confirm/", {"manual": "1", "note": "المستفيد بلا جوال"})
        other.refresh_from_db()
        self.assertEqual((other.receipt_method, other.receipt_note), ("manual", "المستفيد بلا جوال"))
        for url in ("/app/aid-disbursement/new/", f"/app/aid-disbursement/new/?beneficiary={self.family.pk}",
                    "/app/aid-program/", "/beneficiaries/", f"/app/beneficiary/{self.family.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_send_fails_cleanly_without_sms_provider(self):
        mock.patch.stopall()
        doc = self.aid(self.cash_program, 100, confirm=False)
        self.client.force_login(self.user)
        response = self.client.post(f"/beneficiaries/aid/{doc.pk}/send-otp/", follow=True)
        self.assertContains(response, "غير مفعّل")
        doc.refresh_from_db()
        self.assertIsNone(doc.otp_sent_at)  # لا يُحفظ رمز لم يُرسل
