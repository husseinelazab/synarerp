from decimal import Decimal

from django.test import TestCase

from buying.models import PurchaseInvoice, Supplier
from core.models import Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup


class IntercompanyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent, cls.owner = make_company("الشركة الأم", "group_owner")
        cls.child, _child_owner = make_company("الشركة التابعة", "child_owner")
        Membership.objects.create(user=cls.owner, company=cls.child, role=Role.OWNER)
        cls.outsider, cls.stranger = make_company("شركة أخرى", "stranger")

    def item(self, company):
        with tenant_context(company):
            return Item.objects.get_or_create(code="IC", defaults={
                "name": "منتج", "item_group": ItemGroup.objects.first(), "uom": UOM.objects.first(),
                "is_stock_item": False})[0]

    def test_mirror_and_consolidation(self):
        self.item(self.child)
        with tenant_context(self.parent):
            item = self.item(self.parent)
            customer = Customer.objects.create(name="التابعة", represents_company=self.child)
            inv = SalesInvoice.objects.create(customer=customer, update_stock=False, created_by=self.owner)
            inv.items.create(item=item, qty=2, rate=500)
            inv.calculate_totals(); inv.save(); inv.submit()
        with tenant_context(self.child):
            bill = PurchaseInvoice.objects.get()
            self.assertEqual((bill.intercompany_reference, bill.supplier.represents_company), (inv.name, self.parent))
            self.assertEqual(bill.grand_total, inv.grand_total)
            bill.submit()
        self.client.force_login(self.owner)
        page = self.client.get(f"/reports/accounts/consolidated/?kind=pl&company={self.parent.pk}&company={self.child.pk}")
        income = page.context["sections"][0]
        self.assertEqual(income["totals"][[c.pk for c in page.context["companies"]].index(self.parent.pk)],
                         Decimal("1000"))
        self.assertEqual(income["total"], 0)  # المبيعات بين الشركتين مستبعدة
        self.assertEqual(page.context["result"]["total"], 0)
        bs = self.client.get(f"/reports/accounts/consolidated/?kind=bs&company={self.parent.pk}&company={self.child.pk}")
        assets = bs.context["sections"][0]
        receivable = [r for r in assets["rows"] if r["key"] == "1103"][0]
        self.assertEqual(receivable["total"], 0)
        self.assertNotIn(self.outsider, bs.context["available"])

    def test_customer_form_lists_only_managed_companies(self):
        self.client.force_login(self.owner)
        page = self.client.get("/app/customer/new/").content.decode()
        self.assertIn("الشركة التابعة", page)
        self.assertNotIn("شركة أخرى", page)
