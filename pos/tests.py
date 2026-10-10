import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase

from accounts.models import GLEntry
from buying.models import PurchaseInvoice, Supplier
from core.models import Company, DocStatus
from core.zatca import decode_payload, qr_payload
from pos.models import POSProfile, POSShift
from pos.services import checkout
from selling.models import SalesInvoice
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse


def gl_balance(**filters):
    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or 0) - (agg["c"] or 0)


class POSTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.get_default()
        cls.profile = POSProfile.objects.get()
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.user = User.objects.create_user("cashier", password="pass12345")
        group, uom = ItemGroup.objects.first(), UOM.objects.first()
        cls.item = Item.objects.create(code="W1", name="ماء", item_group=group, uom=uom, selling_rate=115, buying_rate=50)
        cls.service = Item.objects.create(code="S1", name="تغليف", item_group=group, uom=uom, selling_rate=23,
                                          is_stock_item=False)
        inv = PurchaseInvoice.objects.create(supplier=Supplier.objects.create(name="مورد"), warehouse=cls.wh)
        inv.items.create(item=cls.item, qty=10, rate=50)
        inv.submit()

    def setUp(self):
        self.shift = POSShift.open(self.profile, self.user, opening_cash=200)

    def sell(self, qty=1, **kwargs):
        kwargs.setdefault("paid_cash", 1000)
        return checkout(self.shift, self.user, [{"item": self.item.id, "qty": qty}], **kwargs)

    def test_inclusive_tax_totals_and_gl(self):
        inv = self.sell(2)  # 2 × 115 شامل الضريبة
        self.assertTrue(inv.prices_include_tax)
        self.assertEqual(inv.grand_total, Decimal("230.00"))
        self.assertEqual(inv.tax_amount, Decimal("30.00"))
        self.assertEqual(inv.net_total, Decimal("200.00"))
        self.assertEqual(gl_balance(account=self.company.default_income_account), Decimal("-200"))
        self.assertEqual(gl_balance(account=self.company.default_output_tax_account), Decimal("-30"))
        self.assertEqual(gl_balance(), 0)

    def test_inclusive_discount_posts_ex_tax(self):
        inv = self.sell(2, discount=23)
        self.assertEqual(inv.grand_total, Decimal("207.00"))
        self.assertEqual(inv.tax_amount, Decimal("27.00"))
        self.assertEqual(gl_balance(account=self.company.default_discount_account), Decimal("20"))
        self.assertEqual(gl_balance(account=self.company.default_income_account), Decimal("-200"))
        self.assertEqual(gl_balance(), 0)

    def test_payment_change_and_receivable_settled(self):
        inv = self.sell(1, paid_cash=200)
        self.assertEqual(inv.change_amount, Decimal("85.00"))
        self.assertEqual(inv.outstanding_amount, 0)
        self.assertEqual(inv.docstatus, DocStatus.SUBMITTED)
        self.assertEqual(gl_balance(account=self.profile.cash_account), Decimal("115"))
        self.assertEqual(gl_balance(customer=self.profile.default_customer), 0)
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.wh).actual_qty, 9)

    def test_split_payment_cash_and_card(self):
        checkout(self.shift, self.user, [{"item": self.item.id, "qty": 1}, {"item": self.service.id, "qty": 1}],
                 paid_cash=38, paid_card=100)
        self.assertEqual(gl_balance(account=self.profile.card_account), Decimal("100"))
        self.assertEqual(gl_balance(account=self.profile.cash_account), Decimal("38"))
        self.assertEqual(gl_balance(), 0)

    def test_insufficient_payment_rolls_back(self):
        with self.assertRaises(ValidationError):
            self.sell(1, paid_cash=100)
        self.assertFalse(SalesInvoice.objects.exists())
        self.assertEqual(Bin.objects.get(item=self.item, warehouse=self.wh).actual_qty, 10)

    def test_card_overpayment_rejected(self):
        with self.assertRaises(ValidationError):
            self.sell(1, paid_cash=0, paid_card=200)

    def test_insufficient_stock_rejected(self):
        with self.assertRaises(ValidationError):
            self.sell(11)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_discount_limit(self):
        with self.assertRaises(ValidationError):
            self.sell(1, discount=50)  # الحد 10%

    def test_price_cannot_be_overridden_by_default(self):
        inv = checkout(self.shift, self.user, [{"item": self.item.id, "qty": 1, "rate": 1}], paid_cash=500)
        self.assertEqual(inv.grand_total, Decimal("115.00"))

    def test_shift_close_reconciliation(self):
        self.sell(1, paid_cash=200)                     # نقداً 115
        self.sell(1, paid_cash=0, paid_card=115)        # بطاقة 115
        summary = self.shift.summary()
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["cash"], Decimal("115"))
        self.assertEqual(summary["card"], Decimal("115"))
        self.assertEqual(summary["expected_cash"], Decimal("315"))
        self.shift.close(closing_cash=310)
        self.assertEqual(self.shift.difference, Decimal("-5"))
        with self.assertRaises(ValidationError):
            self.sell(1)

    def test_cannot_cancel_after_shift_closed(self):
        inv = self.sell(1)
        self.shift.close(closing_cash=0)
        with self.assertRaises(ValidationError):
            inv.cancel()

    def test_one_open_shift_per_user(self):
        with self.assertRaises(ValidationError):
            POSShift.open(self.profile, self.user)

    def test_zatca_qr_payload(self):
        payload = qr_payload("شركة", "310122393500003", "2026-10-10T12:00:00+03:00", Decimal("230"), Decimal("30"))
        self.assertEqual(decode_payload(payload), {
            1: "شركة", 2: "310122393500003", 3: "2026-10-10T12:00:00+03:00", 4: "230.00", 5: "30.00",
        })


class POSViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.user = User.objects.create_user("cashier", password="pass12345")

    def setUp(self):
        self.client.force_login(self.user)

    def test_full_pos_flow_over_http(self):
        self.assertContains(self.client.get("/pos/"), "فتح الوردية")
        self.client.post("/pos/open/", {"profile": POSProfile.objects.get().pk, "opening_cash": "100"})
        self.assertContains(self.client.get("/pos/"), "pos-config")
        items = self.client.get("/pos/api/items/").json()["items"]
        mouse = next(i for i in items if i["code"] == "MOU-001")
        response = self.client.post("/pos/api/checkout/", json.dumps({
            "items": [{"item": mouse["id"], "qty": 2}], "paid_cash": 200,
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content.decode())
        data = response.json()
        self.assertEqual(data["grand_total"], "150.00")
        self.assertEqual(data["change"], "50.00")
        receipt = self.client.get(data["receipt_url"])
        self.assertContains(receipt, "فاتورة ضريبية مبسطة")
        self.assertContains(receipt, "<svg")

        bad = self.client.post("/pos/api/checkout/", json.dumps({"items": []}), content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("السلة فارغة", bad.json()["error"])

        self.assertContains(self.client.get("/pos/close/"), "النقدية المتوقعة")
        self.client.post("/pos/close/", {"closing_cash": "250"})
        shift = POSShift.objects.get()
        self.assertFalse(shift.is_open)
        self.assertEqual(shift.difference, 0)
        self.assertContains(self.client.get(f"/pos/shifts/{shift.pk}/"), "فأرة لاسلكية")
        self.assertEqual(self.client.get("/pos/shifts/").status_code, 200)

    def test_checkout_without_shift(self):
        response = self.client.post("/pos/api/checkout/", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)
