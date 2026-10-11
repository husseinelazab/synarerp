import datetime
from decimal import Decimal

from django.test import TestCase

from accounts.models import Account, PaymentEntry
from core.models import Membership
from core.tenancy import tenant_context
from hr.models import Employee, PayrollRun, sales_commission
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse

TODAY = datetime.date.today()
START = TODAY.replace(day=1)


class CommissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة العمولات", "commboss")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.rep = Employee.objects.create(employee_number="S1", full_name="مندوب", gender="male",
                                           date_of_joining=START - datetime.timedelta(days=500), basic_salary=4000,
                                           gosi_subscribed=False, commission_rate=2)
        Membership.objects.filter(company=self.company, user=self.user).update(employee=self.rep)
        self.item = Item.objects.create(code="CM", name="خدمة", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), is_stock_item=False)
        self.customer = Customer.objects.create(name="عميل")

    def sell(self, rate, **kwargs):
        inv = SalesInvoice.objects.create(customer=self.customer, warehouse=Warehouse.objects.first(), update_stock=False,
                                          created_by=self.user, posting_date=START, **kwargs)
        inv.items.create(item=self.item, qty=1, rate=rate)
        inv.submit()
        return inv

    def test_sales_person_defaults_and_invoiced_commission(self):
        inv = self.sell(10000)
        self.assertEqual(inv.sales_person, self.rep)
        ret = inv.make_return(self.user)
        ret.items.update(qty=-1)
        ret.calculate_totals()
        ret.save()
        ret.submit()
        self.assertEqual(ret.sales_person, self.rep)                       # المرتجع يخصم من عمولته
        self.sell(5000)
        result = sales_commission(self.rep, START, TODAY)
        self.assertEqual((result["invoiced"], result["commission"]), (Decimal("5000.00"), Decimal("100.00")))

    def test_collected_basis_excludes_vat(self):
        self.rep.commission_basis = "collected"
        self.rep.save()
        inv = self.sell(1000)                                              # 1150 شامل الضريبة
        pay = PaymentEntry.objects.create(payment_type="receive", customer=self.customer, sales_invoice=inv,
                                          account=Account.objects.get(number="1102"), amount=575, posting_date=TODAY)
        pay.submit()
        result = sales_commission(self.rep, START, TODAY)
        self.assertEqual((result["collected"], result["commission"]), (Decimal("500.00"), Decimal("10.00")))

    def test_commission_added_to_payroll_and_report(self):
        self.sell(20000)
        run = PayrollRun.objects.create(year=TODAY.year, month=TODAY.month, posting_date=TODAY)
        run.calculate_totals()
        run.save()
        line = run.lines.get(employee=self.rep)
        self.assertEqual(line.commission, Decimal("400.00"))
        self.assertEqual(line.gross_pay, Decimal("4400.00"))
        self.client.force_login(self.user)
        page = self.client.get("/hr/commissions/")
        self.assertEqual(page.context["total"], Decimal("400.00"))
        self.assertContains(self.client.get(f"/hr/payroll/{run.pk}/payslips/"), "عمولة المبيعات")
