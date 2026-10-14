import datetime
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Fund, GLEntry
from awqaf.models import InstallmentStatus, LeaseStatus, RentInstallment, Waqf, WaqfDistribution, WaqfLease
from beneficiaries.tests import make_nonprofit
from core.models import Notification
from core.tenancy import tenant_context
from funds.models import DonationReceipt
from funds.reports import activities_data

TODAY = timezone.localdate()


class WaqfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الأوقاف الخيرية", "awqaf")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.waqf = Waqf.objects.create(name="عمارة الخير", waqif="فاعل خير", deed_number="123", asset_value=2000000,
                                        revenue_restriction="restricted")
        self.reserve = Fund.objects.create(name="احتياطي عمارة الخير", fund_type="restricted")
        self.orphans = Fund.objects.get(name="صندوق الكفالات")
        for name, percent, fund in (("النظارة", 10, None), ("عمارة الوقف", 20, self.reserve),
                                    ("الأيتام", 70, self.orphans)):
            self.waqf.shares.create(name=name, percent=percent, target_fund=fund)

    def lease(self, **kwargs):
        lease = WaqfLease.objects.create(waqf=kwargs.pop("waqf", self.waqf), unit=kwargs.pop("unit", "معرض 1"), tenant_name="مؤسسة النور",
                                         start_date=kwargs.pop("start_date", TODAY.replace(day=1)),
                                         end_date=kwargs.pop("end_date", TODAY.replace(day=1) + datetime.timedelta(days=364)),
                                         annual_rent=kwargs.pop("annual_rent", 120000), **kwargs)
        lease.activate(self.user)
        return lease

    def collect(self, installment, amount=None):
        receipt = installment.collect(self.user)
        if amount:
            receipt.amount = amount
            receipt.save()
        receipt.submit()
        installment.refresh_from_db()
        return receipt

    def test_waqf_creates_principal_and_revenue_funds(self):
        self.assertEqual((self.waqf.principal_fund.fund_type, self.waqf.revenue_fund.fund_type), ("endowment",
                                                                                                  "restricted"))
        self.assertEqual(self.waqf.revenue_fund.name, "غلة عمارة الخير")
        donation = DonationReceipt(donation_type="endowment", fund=self.waqf.principal_fund, amount=50000,
                                   account=self.company.default_cash_account)
        donation.full_clean()
        donation.save()
        donation.calculate_totals()
        donation.submit()
        self.assertEqual(self.waqf.principal_balance, Decimal("50000"))

    def test_lease_schedule_and_rent_collection(self):
        lease = self.lease(frequency="quarterly")
        self.assertEqual(lease.status, LeaseStatus.ACTIVE)
        self.assertEqual(list(lease.installments.values_list("amount", flat=True)), [Decimal("30000")] * 4)
        with self.assertRaisesMessage(ValidationError, "مؤجرة بعقد ساري"):
            self.lease(frequency="annual")
        first = lease.installments.first()
        receipt = self.collect(first, 10000)
        self.assertEqual(first.status, InstallmentStatus.PARTIAL)
        income = GLEntry.objects.get(voucher_type="rent-receipt", voucher_id=receipt.pk, credit__gt=0)
        self.assertEqual((income.account.number, income.fund), ("31303001", self.waqf.revenue_fund))
        self.collect(first)
        self.assertEqual(first.status, InstallmentStatus.PAID)
        self.assertEqual(self.waqf.revenue_available, Decimal("30000"))
        lease.terminate(self.user)
        self.assertTrue(lease.installments.filter(status=InstallmentStatus.CANCELLED).exists())

    def test_distribution_follows_waqif_conditions(self):
        self.collect(self.lease(frequency="annual").installments.get())
        distribution = WaqfDistribution.objects.create(waqf=self.waqf)
        distribution.calculate_totals()
        distribution.save()
        self.assertEqual(distribution.amount, Decimal("120000"))
        self.assertEqual([l.amount for l in distribution.lines.all()],
                         [Decimal("12000"), Decimal("24000"), Decimal("84000")])
        distribution.submit()
        self.assertEqual(self.waqf.revenue_available, 0)
        self.assertEqual(self.reserve.balance, Decimal("24000"))
        self.assertEqual(self.orphans.balance, Decimal("84000"))
        numbers = set(GLEntry.objects.filter(voucher_type="waqf-distribution").values_list("account__number", flat=True))
        self.assertEqual(numbers, {"47101001", "31401001"})
        start = TODAY.replace(month=1, day=1)
        data = activities_data(start, TODAY)
        self.assertEqual(data["net_total"], Decimal("120000"))  # التحويل لا يغير صافي الأصول الكلي
        again = WaqfDistribution.objects.create(waqf=self.waqf, amount=1000)
        again.calculate_totals()
        again.save()
        with self.assertRaisesMessage(ValidationError, "لا يكفي"):
            again.submit()

    def test_unrestricted_revenue_by_default(self):
        general = Waqf.objects.create(name="وقف البر العام", asset_value=500000)
        self.assertEqual(general.revenue_fund.fund_type, "unrestricted")
        share = general.shares.model(parent=general, name="الأيتام", percent=100, target_fund=self.orphans)
        with self.assertRaisesMessage(ValidationError, "غير مقيدة"):
            share.full_clean()
        # الغلة تُصنف غير مقيدة في قائمة الأنشطة
        lease = self.lease(waqf=general, unit="محل 2", annual_rent=12000, frequency="annual")
        self.collect(lease.installments.first())
        net = activities_data(TODAY.replace(month=1, day=1), TODAY)["net"]
        self.assertEqual(net[0], Decimal("12000"))  # غير مقيدة
        self.assertEqual(GLEntry.objects.get(account__number="31303001", fund=general.revenue_fund).credit,
                         Decimal("12000"))
        general.revenue_restriction = "restricted"
        with self.assertRaisesMessage(ValidationError, "لا يُغيَّر تصنيف الغلة"):
            general.full_clean()

    def test_shares_must_total_100(self):
        self.waqf.shares.filter(name="النظارة").delete()
        distribution = WaqfDistribution.objects.create(waqf=self.waqf, amount=100)
        with self.assertRaisesMessage(ValidationError, "100%"):
            distribution.submit()

    def test_alerts_dashboard_and_pages(self):
        start = TODAY.replace(day=1) - datetime.timedelta(days=400)
        lease = self.lease(unit="شقة 2", start_date=start, end_date=TODAY + datetime.timedelta(days=30),
                           frequency="annual", annual_rent=24000)
        call_command("waqf_alerts", stdout=StringIO())
        titles = list(Notification.objects.values_list("title", flat=True))
        self.assertTrue(any("ينتهي" in t for t in titles))
        self.assertTrue(any("متأخر" in t for t in titles))
        self.client.force_login(self.user)
        html = self.client.get("/awqaf/").content.decode()
        self.assertIn("عمارة الخير", html)
        self.assertIn("إدارة الأوقاف", html)
        installment = RentInstallment.objects.filter(lease=lease).first()
        response = self.client.post(f"/app/rent-installment/{installment.pk}/collect/")
        self.assertEqual(response.status_code, 302)
        for url in (f"/app/waqf/{self.waqf.pk}/", f"/app/waqf-lease/{lease.pk}/", "/app/waqf/new/",
                    "/app/waqf-lease/new/", "/app/rent-receipt/new/", "/app/waqf-distribution/new/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
