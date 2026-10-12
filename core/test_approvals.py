from django.contrib.auth.models import User
from django.test import TestCase

from buying.models import PurchaseOrder, Supplier
from core.models import ApprovalRecord, ApprovalRule, Membership, Notification, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from stock.models import UOM, Item, ItemGroup


class ApprovalTests(TestCase):
    """أمر شراء فوق 50 ألفاً يعتمده المدير ثم المالك، وما دونه يُعتمد مباشرة."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة الاعتماد", "approval_owner")
        cls.manager = User.objects.create_user("approval_manager", password="x")
        Membership.objects.create(user=cls.manager, company=cls.company, role=Role.ADMIN)
        cls.clerk = User.objects.create_user("approval_clerk", password="x")
        Membership.objects.create(user=cls.clerk, company=cls.company, role=Role.USER)

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.item = Item.objects.create(code="AP", name="معدة", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first())
        self.supplier = Supplier.objects.create(name="مورد")
        ApprovalRule.objects.create(doctype="purchase-order", level=1, min_amount=50000, approver_role="manager")
        ApprovalRule.objects.create(doctype="purchase-order", level=2, min_amount=50000, approver_role="owner")

    def order(self, rate):
        po = PurchaseOrder.objects.create(supplier=self.supplier, tax_rate=0)
        po.items.create(item=self.item, qty=1, rate=rate)
        po.calculate_totals()
        po.save()
        return po

    def submit_as(self, user, po):
        self.client.force_login(user)
        return self.client.post(f"/app/purchase-order/{po.pk}/submit/", follow=True)

    def test_two_level_chain(self):
        po = self.order(80000)
        page = self.submit_as(self.clerk, po)
        po.refresh_from_db()
        self.assertTrue(po.is_draft)
        self.assertContains(page, "بانتظار اعتماد المستوى 1")
        self.assertTrue(Notification.objects.filter(user=self.manager, title__contains="بانتظار اعتمادك").exists())
        self.submit_as(self.manager, po)
        po.refresh_from_db()
        self.assertTrue(po.is_draft)
        self.assertEqual(ApprovalRecord.objects.get().level, 1)
        self.assertTrue(Notification.objects.filter(user=self.owner, title__contains="المستوى 2").exists())
        page = self.submit_as(self.owner, po)
        po.refresh_from_db()
        self.assertTrue(po.is_submitted)

    def test_owner_approves_all_levels_at_once(self):
        po = self.order(60000)
        self.submit_as(self.owner, po)  # المالك مدير أيضاً فيملك المستويين
        po.refresh_from_db()
        self.assertTrue(po.is_submitted)

    def test_below_threshold_and_edit_resets(self):
        small = self.order(1000)
        self.submit_as(self.clerk, small)
        small.refresh_from_db()
        self.assertTrue(small.is_submitted)
        po = self.order(70000)
        self.submit_as(self.manager, po)
        self.assertEqual(ApprovalRecord.objects.count(), 1)
        self.client.force_login(self.clerk)
        data = {"supplier": self.supplier.pk, "posting_date": po.posting_date.isoformat(), "tax_rate": "0",
                "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1", "items-MIN_NUM_FORMS": "1",
                "items-MAX_NUM_FORMS": "1000", "items-0-id": po.items.get().pk, "items-0-item": self.item.pk,
                "items-0-qty": "2", "items-0-rate": "70000"}
        self.client.post(f"/app/purchase-order/{po.pk}/edit/", data)
        self.assertEqual(ApprovalRecord.objects.count(), 0)

    def test_rules_page_manager_only(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/app/approval-rule/new/"), "أمر شراء")
        self.assertContains(self.client.get("/app/approval-rule/"), "أمر شراء")
        self.client.force_login(self.clerk)
        self.assertEqual(self.client.get("/app/approval-rule/").status_code, 302)
