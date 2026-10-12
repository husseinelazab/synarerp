from decimal import Decimal

from django.test import TestCase

from core.tenancy import tenant_context
from hr.models import Employee, PayrollRun
from saas.tests import make_company
from selling.models import Customer, SalesInvoice, SalesPerson, Territory
from stock.models import UOM, Item, ItemGroup


class CommissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة المندوبين", "salesteam")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.item = Item.objects.create(code="SP", name="خدمة", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), is_stock_item=False, vat_category="Z",
                                        vat_exemption_reason="VATEX-SA-32")
        self.riyadh = Territory.objects.create(name="الرياض")
        self.person = SalesPerson.objects.create(name="خالد", commission_rate=5, territory=self.riyadh,
                                                 monthly_target=1000)
        self.customer = Customer.objects.create(name="عميل خالد", sales_person=self.person, territory=self.riyadh)

    def invoice(self, amount):
        inv = SalesInvoice.objects.create(customer=self.customer, tax_rate=0, vat_category="Z",
                                          vat_exemption_reason="VATEX-SA-32", update_stock=False)
        inv.items.create(item=self.item, qty=1, rate=amount)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def test_commission_from_customer_person_and_return(self):
        inv = self.invoice(2000)
        self.assertEqual((inv.sales_person, inv.commission_amount), (self.person, Decimal("100")))
        self.person.commission_rate = 10
        self.person.save()
        ret = inv.make_return()
        ret.submit()
        self.assertEqual(ret.commission_amount, Decimal("-100"))  # بنسبة الفاتورة الأصلية

    def test_report_and_payroll(self):
        from hr.models import Department, Designation

        self.invoice(3000)
        employee = Employee.objects.create(employee_number="E1", full_name="خالد", gender="male",
                                           department=Department.objects.first(),
                                           designation=Designation.objects.first(), basic_salary=5000,
                                           date_of_joining="2026-01-01")
        self.person.employee = employee
        self.person.save()
        run = PayrollRun.objects.create()
        run.fetch_employees()
        self.client.force_login(self.user)
        page = self.client.get("/selling/sales-by-person/")
        row = page.context["rows"][0]
        self.assertEqual((row["net"], row["commission"]), (Decimal("3000"), Decimal("150")))
        self.assertEqual(page.context["by_territory"][0]["territory"], "الرياض")
        url = "/selling/sales-by-person/"
        self.client.post(url, {"payroll_run": run.pk})
        self.client.post(url, {"payroll_run": run.pk})  # لا تُضاف مرتين
        line = run.lines.get(employee=employee)
        self.assertEqual(line.additions, Decimal("150"))
