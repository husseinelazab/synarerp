import datetime

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from buying.models import PurchaseInvoice, Supplier
from core.models import Notification
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, SerialNo, Warehouse
from support.models import MaintenanceContract, ServiceTicket


class SupportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الصيانة", "support")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.customer = Customer.objects.create(name="عميل الصيانة")
        self.today = timezone.localdate()

    def sold_serial(self):
        item = Item.objects.create(code="AC", name="مكيف", item_group=ItemGroup.objects.first(), uom=UOM.objects.first(),
                                   has_serial_no=True, warranty_days=365)
        wh = Warehouse.objects.first()
        pinv = PurchaseInvoice.objects.create(supplier=Supplier.objects.create(name="م"), warehouse=wh)
        pinv.items.create(item=item, qty=1, rate=1000, serial_nos="AC-1")
        pinv.calculate_totals(); pinv.save(); pinv.submit()
        sinv = SalesInvoice.objects.create(customer=self.customer, warehouse=wh)
        sinv.items.create(item=item, qty=1, rate=1500, serial_nos="AC-1")
        sinv.calculate_totals(); sinv.save(); sinv.submit()
        return SerialNo.objects.get(serial_no="AC-1")

    def test_warranty_coverage_and_sla(self):
        serial = self.sold_serial()
        ticket = ServiceTicket.objects.create(subject="لا يبرد", customer=self.customer, serial_no=serial, priority="urgent")
        self.assertTrue(ticket.covered)
        self.assertEqual(ticket.item, serial.item)
        self.assertNotIn("make_invoice", dict(ticket.available_actions))
        ticket.resolve()
        self.assertTrue(ticket.met_sla)

    def test_paid_ticket_invoice_and_overdue_alert(self):
        ticket = ServiceTicket.objects.create(subject="تسريب", customer=self.customer, priority="urgent",
                                              opened_at=timezone.now() - datetime.timedelta(hours=10))
        self.assertFalse(ticket.covered)
        self.assertTrue(ticket.is_overdue)
        invoice = ticket.make_invoice()
        self.assertEqual(invoice.customer, self.customer)
        call_command("support_alerts", stdout=open("/dev/null", "w"))
        self.assertTrue(Notification.objects.filter(title__contains="تذكرة متأخرة").exists())

    def test_contract_covers_and_schedules_visits(self):
        contract = MaintenanceContract.objects.create(name="عقد سنوي", customer=self.customer, start_date=self.today,
                                                      end_date=self.today + datetime.timedelta(days=364),
                                                      visits_per_year=4, amount=6000)
        contract.schedule_visits()
        self.assertEqual(contract.visits.count(), 4)
        ticket = ServiceTicket.objects.create(subject="صيانة", customer=self.customer)
        self.assertTrue(ticket.covered)
        self.client.force_login(self.user)
        page = self.client.get("/support/")
        self.assertEqual(len(page.context["open_tickets"]), 1)
        for slug in ("service-ticket", "maintenance-contract", "maintenance-visit"):
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200)
        self.assertEqual(self.client.get(ticket.get_absolute_url()).status_code, 200)
