import datetime
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Fund, GLEntry
from beneficiaries.models import AidProgram, Beneficiary, BeneficiaryCategory, BeneficiaryStatus
from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from funds.models import (DonationReceipt, Donor, DueStatus, Sponsorship, SponsorshipDue, SponsorshipStatus,
                          SponsorshipType, generate_dues)

TODAY = timezone.localdate()
MONTH = TODAY.replace(day=1)


class SponsorshipTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية كافل اليتيم", "kafil")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.orphan = Beneficiary.objects.create(full_name="يتيم", national_id="1200000001", phone="0551110001",
                                                 category=BeneficiaryCategory.objects.get(name="أيتام"),
                                                 status=BeneficiaryStatus.APPROVED,
                                                 next_review_date=TODAY + datetime.timedelta(days=300))
        self.donor = Donor.objects.create(name="أبو عبدالله", phone="0559990001")
        self.orphan_type = SponsorshipType.objects.get(name="كفالة يتيم")

    def sponsor(self, **kwargs):
        sponsorship = Sponsorship(donor=self.donor, beneficiary=kwargs.pop("beneficiary", self.orphan),
                                  sponsorship_type=self.orphan_type,
                                  monthly_amount=kwargs.pop("monthly_amount", 400),
                                  start_date=kwargs.pop("start_date", MONTH), **kwargs)
        sponsorship.full_clean()
        sponsorship.save()
        return sponsorship

    def test_seeded_types_program_and_fund(self):
        program = AidProgram.objects.get(name="الكفالات")
        self.assertEqual((program.expense_account.number, program.fund.name), ("42102001", "صندوق الكفالات"))
        self.assertEqual(self.orphan_type.aid_program, program)
        self.assertEqual(list(self.orphan_type.categories.values_list("name", flat=True)), ["أيتام"])
        self.assertEqual(SponsorshipType.objects.count(), 3)

    def test_rules(self):
        self.sponsor()
        with self.assertRaisesMessage(ValidationError, "مكفول بالفعل"):
            self.sponsor()
        family = Beneficiary.objects.create(full_name="أسرة", national_id="1200000002", phone="1",
                                            category=BeneficiaryCategory.objects.get(name="أسر محتاجة"),
                                            status=BeneficiaryStatus.APPROVED)
        with self.assertRaisesMessage(ValidationError, "ليست من فئات"):
            self.sponsor(beneficiary=family)
        family.status = BeneficiaryStatus.NEW
        family.save()
        with self.assertRaisesMessage(ValidationError, "المعتمدين فقط"):
            Sponsorship(donor=self.donor, beneficiary=family, start_date=MONTH,
                        sponsorship_type=SponsorshipType.objects.get(name="كفالة أسرة")).full_clean()

    def test_monthly_cycle_collect_and_pay(self):
        sponsorship = self.sponsor()
        self.assertTrue(sponsorship.number.startswith("SPN-"))
        self.assertEqual(generate_dues(TODAY), 1)
        self.assertEqual(generate_dues(TODAY), 0)  # لا يتكرر
        due = sponsorship.dues.get()
        receipt = due.collect(self.user)
        self.assertEqual((receipt.donation_type, receipt.fund.name, receipt.amount, receipt.donor),
                         ("sponsorship", "صندوق الكفالات", Decimal("400"), self.donor))
        receipt.calculate_totals()
        receipt.submit()
        due.refresh_from_db()
        self.assertEqual((due.status, due.collected_amount), (DueStatus.PAID, Decimal("400")))
        income = GLEntry.objects.get(voucher_type="donation-receipt", voucher_id=receipt.pk, credit__gt=0)
        self.assertEqual(income.account.number, "31102001")
        aid = due.pay_beneficiary(self.user)
        self.assertEqual((aid.amount, aid.program.name, aid.fund.name), (Decimal("400"), "الكفالات", "صندوق الكفالات"))
        aid.verify_otp(aid.issue_otp(), self.user)
        aid.submit()
        self.assertEqual(Fund.objects.get(name="صندوق الكفالات").balance, 0)
        receipt.cancel()
        due.refresh_from_db()
        self.assertEqual(due.status, DueStatus.PENDING)

    def test_partial_collection_and_waiver(self):
        due = SponsorshipDue.objects.create(sponsorship=self.sponsor(), period=MONTH, amount=400)
        receipt = due.collect(self.user)
        receipt.amount = 150
        receipt.save()
        receipt.calculate_totals()
        receipt.submit()
        due.refresh_from_db()
        self.assertEqual((due.status, due.outstanding), (DueStatus.PARTIAL, Decimal("250")))
        due.waive(self.user)
        self.assertNotIn("collect", dict(due.available_actions))

    def test_coverage_and_status(self):
        sponsorship = self.sponsor(start_date=MONTH + datetime.timedelta(days=40))
        self.assertEqual(generate_dues(TODAY), 0)  # تبدأ الشهر القادم
        sponsorship.start_date = MONTH
        sponsorship.save()
        sponsorship.pause(self.user)
        self.assertEqual(generate_dues(TODAY), 0)
        sponsorship.resume(self.user)
        sponsorship.end(self.user)
        self.assertEqual(sponsorship.status, SponsorshipStatus.ENDED)
        self.assertEqual(generate_dues(TODAY), 0)

    def test_command_dashboard_and_statement(self):
        last_month = (MONTH - datetime.timedelta(days=1)).replace(day=1)
        sponsorship = self.sponsor(start_date=last_month)
        SponsorshipDue.objects.create(sponsorship=sponsorship, period=last_month, amount=400)
        out = StringIO()
        call_command("sponsorship_dues", stdout=out)
        self.assertEqual(sponsorship.dues.count(), 2)
        other = Beneficiary.objects.create(full_name="يتيم بلا كافل", national_id="1200000009", phone="1",
                                           category=BeneficiaryCategory.objects.get(name="أيتام"),
                                           status=BeneficiaryStatus.APPROVED)
        self.client.force_login(self.user)
        html = self.client.get("/resources/").content.decode()
        self.assertIn("يتيم بلا كافل", html)
        self.assertIn(sponsorship.number, html)  # المتأخر
        self.assertIn("إدارة الموارد المالية", html)
        home = self.client.get("/").content.decode()
        self.assertIn("التبرعات هذا الشهر", home)
        self.assertIn("استحقاق كفالة متأخر", home)
        self.assertNotIn("صافي المبيعات", home)
        statement = self.client.get(f"/resources/donor/{self.donor.pk}/statement/?year={last_month.year}")
        self.assertContains(statement, "يتيم")
        due = sponsorship.dues.get(period=MONTH)
        response = self.client.post(f"/app/sponsorship-due/{due.pk}/collect/")
        receipt = DonationReceipt.objects.get(sponsorship_due=due)
        self.assertRedirects(response, f"/app/donation-receipt/{receipt.pk}/edit/", fetch_redirect_response=False)
        for url in (f"/app/sponsorship/{sponsorship.pk}/", f"/app/donor/{self.donor.pk}/", "/app/sponsorship/new/",
                    f"/app/sponsorship/new/?beneficiary={other.pk}", "/app/donation-receipt/new/", "/app/donor/new/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
