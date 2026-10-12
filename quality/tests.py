from django.core.exceptions import ValidationError
from django.test import TestCase

from buying.models import PurchaseReceipt, Supplier
from core.tenancy import tenant_context
from quality.models import QualityInspection, QualityTemplate
from saas.tests import make_company
from stock.models import UOM, Item, ItemGroup, Warehouse


class QualityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الجودة", "quality")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        template = QualityTemplate.objects.create(name="فحص التمور")
        template.parameters.create(name="الرطوبة %", min_value=10, max_value=25)
        template.parameters.create(name="سلامة التغليف", criteria="بلا تمزق")
        self.item = Item.objects.create(code="DT", name="تمر سكري", item_group=ItemGroup.objects.first(),
                                        uom=UOM.objects.first(), quality_template=template, inspection_on_receipt=True)
        self.receipt = PurchaseReceipt.objects.create(supplier=Supplier.objects.create(name="مزرعة"),
                                                      warehouse=Warehouse.objects.first())
        self.receipt.items.create(item=self.item, qty=100, rate=20)
        self.receipt.calculate_totals()
        self.receipt.save()

    def inspect(self, humidity, packaging):
        self.assertEqual(self.receipt.draft_actions[0][0], "make_quality_inspection")
        qi = self.receipt.make_quality_inspection()
        self.assertEqual(qi.readings.count(), 2)
        qi.readings.filter(parameter="الرطوبة %").update(value=humidity)
        qi.readings.filter(parameter="سلامة التغليف").update(manual_result=packaging)
        qi.calculate_totals()
        qi.save()
        qi.submit()
        return qi

    def test_receipt_blocked_until_accepted(self):
        with self.assertRaises(ValidationError):
            self.receipt.submit()
        qi = self.inspect(18, "ok")
        self.assertEqual(qi.result, "accepted")
        self.receipt.submit()

    def test_rejected_blocks(self):
        qi = self.inspect(30, "ok")
        self.assertEqual(qi.result, "rejected")
        with self.assertRaises(ValidationError):
            self.receipt.submit()

    def test_pending_readings_cannot_submit(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.receipt.get_absolute_url()), "إنشاء فحص جودة")
        qi = self.receipt.make_quality_inspection()
        with self.assertRaises(ValidationError):
            qi.submit()
        self.assertNotContains(self.client.get(self.receipt.get_absolute_url()), "إنشاء فحص جودة")
        self.assertEqual(self.client.get(qi.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get("/app/quality-template/new/").status_code, 200)
        self.assertFalse(QualityInspection.objects.filter(docstatus=1).exists())
