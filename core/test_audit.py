from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from core.models import AuditLog, Company, Membership, Role
from core.tenancy import _current_user, set_current_user
from selling.models import Customer, SalesInvoice
from stock.models import Item, Warehouse


class AuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.clerk = User.objects.create_user("clerk", password="pass12345")
        Membership.objects.create(user=cls.clerk, company=cls.company, role=Role.USER, modules=["selling"])

    def setUp(self):
        AuditLog.objects.all().delete()
        self.client.force_login(self.owner)

    def test_create_update_delete_through_forms(self):
        response = self.client.post("/app/customer/new/", {
            "name": "عميل مراقب", "customer_type": "company", "phone": "0500000000", "credit_limit": "0",
            "country_code": "SA"})
        self.assertEqual(response.status_code, 302, response.content.decode()[:1500])
        customer = Customer.objects.get(name="عميل مراقب")
        created = AuditLog.objects.get(model_label="selling.customer", action="create")
        self.assertEqual((created.user, created.object_id, created.company), (self.owner, str(customer.pk), self.company))
        self.assertTrue(created.ip)
        data = {"name": "عميل مراقب", "customer_type": "company", "phone": "0555555555", "credit_limit": "5000",
                "country_code": "SA"}
        self.client.post(f"/app/customer/{customer.pk}/edit/", data)
        update = AuditLog.objects.get(model_label="selling.customer", action="update")
        self.assertEqual(update.changes["الجوال"], ["0500000000", "0555555555"])
        self.assertEqual(update.changes["حد الائتمان"], ["0", "5000"])
        self.client.post(f"/app/customer/{customer.pk}/delete/")
        deleted = AuditLog.objects.get(model_label="selling.customer", action="delete")
        self.assertEqual(deleted.changes["الجوال"], "0555555555")

    def test_document_lifecycle_and_child_rows(self):
        token = set_current_user(self.owner)
        try:
            inv = SalesInvoice.objects.create(company=self.company, customer=Customer.objects.first(), update_stock=False)
            row = inv.items.create(item=Item.objects.get(code="SRV-001"), qty=1, rate=150)
            inv.calculate_totals()
            inv.save()  # الإجماليات وحدها لا تُسجل
            row.qty = 3
            row.save()
            inv.submit()
            inv.cancel()
        finally:
            _current_user.reset(token)
        entries = AuditLog.objects.filter(company=self.company).order_by("id")
        actions = [(e.model_label, e.action) for e in entries]
        self.assertEqual(actions, [("selling.salesinvoice", "create"), ("selling.salesinvoiceitem", "create"),
                                   ("selling.salesinvoiceitem", "update"), ("selling.salesinvoice", "submit"),
                                   ("selling.salesinvoice", "cancel")])
        row_update = entries.get(model_label="selling.salesinvoiceitem", action="update")
        self.assertEqual(row_update.changes["الكمية"], ["1", "3"])
        self.assertEqual((row_update.parent_label, row_update.parent_id), ("selling.salesinvoice", str(inv.pk)))
        page = self.client.get(f"/app/sales-invoice/{inv.pk}/")
        self.assertContains(page, "سجل التعديلات (5)")
        self.client.force_login(self.clerk)
        self.assertNotContains(self.client.get(f"/app/sales-invoice/{inv.pk}/"), "سجل التعديلات (")

    def test_noisy_and_sensitive_not_logged(self):
        from core.audit import _value

        class F:
            name = "password_encrypted"

        self.assertEqual(_value(F(), "abc"), "***")
        inv = SalesInvoice.objects.create(company=self.company, customer=Customer.objects.first(), update_stock=True,
                                          warehouse=Warehouse.objects.first())
        inv.items.create(item=Item.objects.get(code="MOU-001"), qty=1, rate=75)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        labels = set(AuditLog.objects.values_list("model_label", flat=True))
        self.assertFalse({"accounts.glentry", "stock.stockledgerentry", "stock.bin", "core.namingseries"} & labels)

    def test_audit_page_filters_and_access(self):
        self.client.post("/app/customer/new/", {"name": "أ", "customer_type": "company", "credit_limit": "0",
                                                "country_code": "SA"})
        page = self.client.get("/settings/audit/", {"action": "create", "q": "أ"})
        self.assertContains(page, "إنشاء")
        self.assertEqual(page.context["page"].paginator.count, 1)
        self.client.force_login(self.clerk)
        self.assertNotEqual(self.client.get("/settings/audit/").status_code, 200)
