import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Account, Fund, FundType, GLEntry, JournalEntry
from beneficiaries.tests import make_nonprofit
from core.models import Notification
from core.tenancy import tenant_context
from funds.models import DonationReceipt, Donor, DonorType
from grants.alerts import grants_alerts
from grants.models import (ApplicationStatus, Grant, GrantApplication, GrantInstallment, GrantReport, GrantStatus,
                           ReportStatus)

TODAY = timezone.localdate()


class GrantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الإحسان", "ihsan")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.cash = self.company.default_cash_account
        self.grantor = Donor.objects.create(name="مؤسسة الملك خالد الخيرية", donor_type=DonorType.ENTITY)
        self.food = Account.objects.get(number="42102004")
        self.aid = Account.objects.get(number="42102002")

    def make_grant(self, **extra):
        values = {"grantor": self.grantor, "title": "مشروع السلال", "approved_amount": Decimal("200000"),
                  "start_date": TODAY - datetime.timedelta(days=30), "end_date": TODAY + datetime.timedelta(days=300)}
        values.update(extra)
        grant = Grant(**values)
        grant.full_clean()
        grant.save()
        return grant

    def spend(self, amount, account, fund, posting_date=TODAY):
        entry = JournalEntry.objects.create(remarks="صرف", posting_date=posting_date)
        entry.accounts.create(account=account, debit=amount, fund=fund)
        entry.accounts.create(account=self.cash, credit=amount)
        entry.calculate_totals()
        entry.submit()
        return entry

    def test_fund_must_be_restricted(self):
        general = Fund.objects.create(name="عام", fund_type=FundType.UNRESTRICTED)
        with self.assertRaises(ValidationError) as ctx:
            self.make_grant(fund=general)
        self.assertIn("fund", ctx.exception.message_dict)
        endowment = Fund.objects.create(name="وقف", fund_type=FundType.ENDOWMENT)
        with self.assertRaises(ValidationError):
            self.make_grant(fund=endowment)
        restricted = Fund.objects.create(name="مقيد", fund_type=FundType.RESTRICTED)
        grant = self.make_grant(fund=restricted)
        with self.assertRaises(ValidationError):  # لكل منحة صندوقها
            self.make_grant(title="منحة أخرى", fund=restricted)
        self.assertEqual(grant.fund, restricted)

    def test_create_fund_action(self):
        grant = self.make_grant(agreement_number="A-1")
        self.assertIn("create_fund", dict(grant.available_actions))
        grant.create_fund(self.user)
        grant.refresh_from_db()
        self.assertEqual(grant.fund.fund_type, FundType.RESTRICTED)
        self.assertEqual(grant.fund.name, "منحة مشروع السلال")
        self.assertEqual(grant.fund.target_amount, Decimal("200000"))
        self.assertEqual(grant.fund.donor, self.grantor.name)
        self.assertNotIn("create_fund", dict(grant.available_actions))

    def test_application_workflow_creates_grant(self):
        application = GrantApplication.objects.create(grantor=self.grantor, title="تأهيل الشباب",
                                                      requested_amount=Decimal("80000"))
        self.assertEqual(dict(application.available_actions).keys(), {"send"})
        application.send(self.user)
        self.assertEqual(application.status, ApplicationStatus.SUBMITTED)
        self.assertEqual(application.submitted_date, TODAY)
        application.review(self.user)
        self.assertNotIn("create_grant", dict(application.available_actions))
        with self.assertRaises(ValidationError):
            application.create_grant(self.user)
        application.approve(self.user)
        self.assertEqual(application.decision_date, TODAY)
        self.assertIn("create_grant", dict(application.available_actions))
        grant = application.create_grant(self.user)
        self.assertEqual((grant.title, grant.grantor, grant.approved_amount, grant.status),
                         ("تأهيل الشباب", self.grantor, Decimal("80000"), GrantStatus.ACTIVE))
        self.assertEqual(grant.application, application)
        self.assertGreater(grant.end_date, grant.start_date)
        self.assertEqual(application.available_actions, [])
        with self.assertRaises(ValidationError):
            application.create_grant(self.user)

    def test_installment_receipt_creates_submitted_grant_receipt(self):
        grant = self.make_grant(agreement_number="KKF-1")
        installment = GrantInstallment.objects.create(grant=grant, due_date=TODAY, amount=Decimal("50000"))
        with self.assertRaises(ValidationError):  # لا صندوق بعد
            installment.receive(self.user)
        grant.create_fund(self.user)
        installment.refresh_from_db()
        installment.receive(self.user)
        receipt = installment.receipt
        self.assertIsInstance(receipt, DonationReceipt)
        self.assertTrue(receipt.is_submitted)
        self.assertEqual((receipt.donation_type, receipt.fund, receipt.donor, receipt.amount),
                         ("grant", grant.fund, self.grantor, Decimal("50000")))
        self.assertEqual(receipt.fund.fund_type, FundType.RESTRICTED)
        entries = GLEntry.objects.filter(voucher_type="donation-receipt", voucher_id=receipt.pk, is_cancelled=False)
        credit = entries.get(credit__gt=0)
        self.assertEqual((credit.fund, credit.credit, credit.account.number[:3]), (grant.fund, Decimal("50000"), "311"))
        self.assertEqual(entries.get(debit__gt=0).account, self.cash)
        self.assertEqual(grant.received_amount, Decimal("50000"))
        self.assertEqual(grant.fund.balance, Decimal("50000"))
        self.assertEqual(self.grantor.donation_total(), Decimal("50000"))  # يظهر في كشف الداعم
        self.assertEqual(installment.available_actions, [])
        with self.assertRaises(ValidationError):
            installment.receive(self.user)

    def test_budget_vs_actual(self):
        grant = self.make_grant()
        grant.create_fund()
        grant.budget_lines.create(description="السلال", account=self.food, amount=Decimal("150000"))
        grant.budget_lines.create(description="مساعدات", account=self.aid, amount=Decimal("50000"))
        other = Fund.objects.create(name="صندوق آخر", fund_type=FundType.RESTRICTED, allow_overspend=True)
        Fund.objects.filter(pk=grant.fund_id).update(allow_overspend=True)
        grant.refresh_from_db()
        self.spend(Decimal("30000"), self.food, grant.fund)
        self.spend(Decimal("1000"), self.food, other)  # صندوق آخر لا يُحسب
        self.spend(Decimal("2000"), Account.objects.get(number="42102001"), grant.fund)  # خارج الموازنة
        self.spend(Decimal("700"), self.food, grant.fund,
                   posting_date=grant.start_date - datetime.timedelta(days=1))  # قبل مدة المنحة
        data = grant.budget_vs_actual()
        food, aid, unbudgeted = data["rows"]
        self.assertEqual((food["budget"], food["actual"], food["variance"]),
                         (Decimal("150000"), Decimal("30000"), Decimal("120000")))
        self.assertEqual(food["percent"], Decimal("20"))
        self.assertEqual((aid["actual"], aid["percent"]), (Decimal("0"), Decimal("0")))
        self.assertTrue(unbudgeted["unbudgeted"])
        self.assertEqual(unbudgeted["actual"], Decimal("2000"))
        self.assertEqual((data["totals"]["budget"], data["totals"]["actual"], data["totals"]["variance"]),
                         (Decimal("200000"), Decimal("32000"), Decimal("168000")))
        self.assertEqual(grant.spent_amount, Decimal("32000"))
        self.assertEqual(grant.burn_rate, Decimal("16"))

    def test_alerts(self):
        grant = self.make_grant()
        GrantReport.objects.create(grant=grant, due_date=TODAY + datetime.timedelta(days=5))
        GrantReport.objects.create(grant=grant, due_date=TODAY - datetime.timedelta(days=2), kind="narrative")
        GrantReport.objects.create(grant=grant, due_date=TODAY + datetime.timedelta(days=20))  # بعيد
        done = GrantReport.objects.create(grant=grant, due_date=TODAY - datetime.timedelta(days=9))
        done.mark_submitted(self.user)
        self.assertEqual(done.status, ReportStatus.SUBMITTED)
        GrantInstallment.objects.create(grant=grant, due_date=TODAY - datetime.timedelta(days=3), amount=1000)
        GrantInstallment.objects.create(grant=grant, due_date=TODAY + datetime.timedelta(days=3), amount=1000)
        self.assertEqual(grants_alerts(self.company), 3)
        titles = list(Notification.objects.filter(user=self.user).values_list("title", flat=True))
        self.assertEqual(len(titles), 3)
        self.assertTrue(any("متأخر للمانح" in t for t in titles))
        self.assertTrue(any("دفعة منحة متأخرة" in t for t in titles))
        self.assertEqual(grants_alerts(self.company), 0)  # لا تكرار

    def test_pages_and_demo(self):
        from grants.demo import seed_demo

        grant = seed_demo(self.company, TODAY)
        self.assertEqual(seed_demo(self.company, TODAY), grant)  # لا تكرار
        self.assertEqual(grant.budget_lines.count(), 3)
        self.assertEqual(grant.received_amount, Decimal("100000"))
        self.assertEqual(grant.reports.count(), 2)
        self.assertEqual(grant.spent_amount, Decimal("27500"))
        self.client.force_login(self.user)
        for url in ("/grants/", f"/grants/{grant.pk}/financial-report/", grant.get_absolute_url(),
                    "/app/grant-application/", "/app/grant-installment/", "/app/grant-report/", "/app/grant/new/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        html = self.client.get(grant.get_absolute_url()).content.decode()
        self.assertIn("الموازنة مقابل الفعلي", html)
        dashboard = self.client.get("/grants/").content.decode()
        self.assertIn('href="/app/grant-application/"', dashboard)  # الوحدة في القائمة الجانبية
        self.assertIn(grant.title, dashboard)
        self.assertIn(grant.grantor.name, self.client.get(f"/grants/{grant.pk}/financial-report/").content.decode())
