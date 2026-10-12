from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


class FinePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الصلاحيات", "perm_owner")
        cls.user = User.objects.create_user("perm_user", password="x")
        cls.member = Membership.objects.create(user=cls.user, company=cls.company, role=Role.USER,
                                               denied_doctypes=["supplier"], read_only_doctypes=["customer"],
                                               can_delete=False, can_export=False, hide_costs=True)

    def setUp(self):
        with tenant_context(self.company):
            self.customer = Customer.objects.create(name="عميل")
        self.client.force_login(self.user)

    def test_denied_and_read_only(self):
        self.assertEqual(self.client.get("/app/supplier/").status_code, 302)
        self.assertEqual(self.client.get("/app/customer/").status_code, 200)
        self.assertEqual(self.client.get(f"/app/customer/{self.customer.pk}/").status_code, 200)
        self.assertEqual(self.client.get("/app/customer/new/").status_code, 302)
        self.assertEqual(self.client.get(f"/app/customer/{self.customer.pk}/edit/").status_code, 302)
        self.assertEqual(self.client.get("/app/item/new/").status_code, 200)

    def test_delete_export_costs(self):
        with tenant_context(self.company):
            from stock.models import UOM, Item, ItemGroup

            item = Item.objects.create(code="Z", name="ص", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                       buying_rate=77)
        self.client.post(f"/app/item/{item.pk}/delete/")
        self.assertTrue(Item.all_objects.filter(pk=item.pk).exists())
        self.assertEqual(self.client.get("/app/item/export/").status_code, 302)
        self.assertEqual(self.client.get("/reports/stock/gross-profit/").status_code, 302)
        page = self.client.get(f"/app/item/{item.pk}/").content.decode()
        self.assertNotIn("سعر الشراء", page)
        menu = self.client.get("/").content.decode()
        self.assertNotIn("/app/supplier/", menu)

    def test_manager_unaffected_and_form(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/app/supplier/").status_code, 200)
        page = self.client.get(f"/settings/users/{self.member.pk}/")
        self.assertContains(page, "مستندات للاطلاع فقط")
