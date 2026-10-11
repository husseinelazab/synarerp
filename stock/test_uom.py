import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from buying.models import PurchaseInvoice, Supplier
from core.models import Company, Membership, Role
from selling.models import Customer, SalesInvoice, SalesOrder
from stock.models import UOM, Bin, Item, ItemUOM, StockCount, StockLedgerEntry, Warehouse

CARTON_BARCODE = "6281234567895"


class UOMTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        ref = Item.objects.get(code="MOU-001")
        cls.piece = ref.uom
        cls.carton = UOM.objects.get_or_create(company=cls.company, name="كرتون")[0]
        cls.pepsi = Item.objects.create(company=cls.company, code="PEP", name="بيبسي", item_group=ref.item_group,
                                        uom=cls.piece, selling_rate=3, buying_rate=2, barcode="6281111111110")
        cls.unit = ItemUOM.objects.create(company=cls.company, item=cls.pepsi, uom=cls.carton, factor=24,
                                          barcode=CARTON_BARCODE, selling_rate=60)
        cls.wh = Warehouse.objects.get(name="المستودع الرئيسي")
        cls.supplier = Supplier.objects.first()
        cls.customer = Customer.objects.first()

    def setUp(self):
        self.client.force_login(self.owner)
        self.pi = self.buy(5, 40, self.carton)  # 120 حبة

    def buy(self, qty, rate, uom=None):
        inv = PurchaseInvoice.objects.create(company=self.company, supplier=self.supplier, warehouse=self.wh)
        inv.items.create(item=self.pepsi, qty=qty, rate=rate, uom=uom)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        return inv

    def stock(self):
        return Bin.objects.get(item=self.pepsi, warehouse=self.wh)

    def test_validation_and_options(self):
        for kwargs in ({"uom": self.piece, "factor": 2}, {"uom": UOM.objects.create(company=self.company, name="درزن"),
                                                          "factor": 0}):
            with self.assertRaises(ValidationError):
                ItemUOM(company=self.company, item=self.pepsi, **kwargs).full_clean()
        dozen = UOM.objects.get(name="درزن")
        with self.assertRaises(ValidationError):
            ItemUOM(company=self.company, item=self.pepsi, uom=dozen, factor=12, barcode="6281111111110").full_clean()
        options = self.pepsi.uom_options()
        self.assertEqual([(o["name"], o["factor"], o["selling_rate"]) for o in options],
                         [(self.piece.name, 1, Decimal("3")), ("كرتون", Decimal("24"), Decimal("60"))])
        self.assertEqual(self.pepsi.price_for(dozen.pk if False else None), Decimal("3"))
        self.assertEqual(self.pepsi.price_for(self.carton.pk, "buying"), Decimal("48.00"))
        with self.assertRaises(ValidationError):
            self.pepsi.uom_factor(dozen.pk)
        api = self.client.get(f"/api/item/{self.pepsi.pk}/").json()
        self.assertEqual(api["uoms"][str(self.carton.pk)]["selling_rate"], "60.00")

    def test_purchase_in_cartons_updates_stock_and_cost_in_pieces(self):
        b = self.stock()
        self.assertEqual((b.actual_qty, b.stock_value), (Decimal("120"), Decimal("200.00")))
        row = self.pi.items.get()
        self.assertEqual((row.stock_qty, row.conversion_factor), (Decimal("120"), Decimal("24")))
        self.pepsi.refresh_from_db()
        self.assertEqual(self.pepsi.buying_rate, Decimal("1.67"))  # آخر سعر شراء للحبة

    def test_sale_in_mixed_units_and_return_in_base(self):
        inv = SalesInvoice.objects.create(company=self.company, customer=self.customer, warehouse=self.wh)
        inv.items.create(item=self.pepsi, qty=2, rate=60, uom=self.carton)
        inv.items.create(item=self.pepsi, qty=3, rate=3)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        self.assertEqual(inv.total, Decimal("129"))
        self.assertEqual(self.stock().actual_qty, Decimal("69"))
        sle = StockLedgerEntry.objects.filter(voucher_type="sales-invoice", voucher_id=inv.pk)
        self.assertEqual(sum(e.actual_qty for e in sle), Decimal("-51"))
        self.assertEqual(inv.returnable_quantities(), {self.pepsi.pk: Decimal("51")})
        ret = inv.make_return(self.owner, {self.pepsi.pk: 24})  # إرجاع 24 حبة
        ret.submit()
        row = ret.items.get()
        self.assertEqual((row.uom_id, row.qty, row.stock_qty), (None, Decimal("-24"), Decimal("-24")))
        self.assertEqual(row.rate, Decimal("2.53"))  # 129 ÷ 51
        self.assertEqual(self.stock().actual_qty, Decimal("93"))
        self.assertEqual(inv.returnable_quantities(), {self.pepsi.pk: Decimal("27")})
        page = self.client.get(f"/app/sales-invoice/{inv.pk}/print/")
        self.assertContains(page, "كرتون")

    def test_order_in_cartons_billed_in_pieces(self):
        so = SalesOrder.objects.create(company=self.company, customer=self.customer, warehouse=self.wh)
        so.items.create(item=self.pepsi, qty=2, rate=60, uom=self.carton)
        so.calculate_totals()
        so.save()
        so.submit()
        dn = so.make_fulfillment(self.owner)
        self.assertEqual(dn.items.get().uom, self.carton)  # الإذن ينسخ الوحدة
        dn.submit()
        self.assertEqual(self.stock().actual_qty, Decimal("72"))
        so_row = so.items.get()
        self.assertEqual(so_row.delivered_qty, Decimal("2"))
        # فاتورة بالحبات مقابل سطر الأمر بالكرتون: 24 حبة = كرتون مفوتر
        inv = SalesInvoice.objects.create(company=self.company, customer=self.customer, warehouse=self.wh,
                                          order=so, update_stock=False)
        inv.items.create(item=self.pepsi, qty=24, rate=2.5, order_item=so_row)
        inv.calculate_totals()
        inv.save()
        inv.submit()
        so_row.refresh_from_db()
        self.assertEqual(so_row.billed_qty, Decimal("1"))

    def test_pos_carton_sale_and_scans(self):
        from pos.models import POSProfile, POSShift
        from pos.services import checkout

        cashier = User.objects.create_user("cashier", password="pass12345")
        Membership.objects.create(user=cashier, company=self.company, role=Role.CASHIER)
        shift = POSShift.open(POSProfile.objects.get(), cashier, opening_cash=0)
        inv = checkout(shift, cashier, [{"item": self.pepsi.pk, "qty": 1, "uom": self.carton.pk}], paid_cash=100)
        row = inv.items.get()
        self.assertEqual((row.rate, row.stock_qty), (Decimal("60"), Decimal("24")))
        self.client.force_login(cashier)
        items = self.client.get("/pos/api/items/").json()["items"]
        units = next(i for i in items if i["id"] == self.pepsi.pk)["units"]
        self.assertEqual(units, [{"uom": self.carton.pk, "name": "كرتون", "factor": 24.0, "barcode": CARTON_BARCODE,
                                  "rate": 60.0}])
        self.client.force_login(self.owner)
        data = self.client.get("/api/item/lookup/", {"code": CARTON_BARCODE}).json()
        self.assertEqual((data["id"], data["uom"], data["uom_name"], data["factor"]), (self.pepsi.pk, self.carton.pk,
                                                                                      "كرتون", "24.000000"))
        # جرد بالماسح: مسح الكرتون = 24 حبة
        count = StockCount.objects.create(company=self.company, warehouse=self.wh)
        count.calculate_totals()
        count.save()
        _, total = count.scan(CARTON_BARCODE, qty=2)
        self.assertEqual(total, Decimal("48"))

    def test_labels_for_carton(self):
        rows = json.dumps([{"id": self.pepsi.pk, "copies": 1, "uom": self.carton.pk}])
        page = self.client.post("/stock/labels/", {"rows": rows, "type": "shelf", "size": "100x50",
                                                   "includes_tax": "1", "show_code": "1"})
        self.assertContains(page, CARTON_BARCODE)
        self.assertContains(page, "لكل كرتون")
        self.assertContains(page, "60.00")

    def test_item_page_and_uom_import(self):
        page = self.client.get(f"/app/item/{self.pepsi.pk}/")
        self.assertContains(page, "وحدات البيع والشراء")
        self.assertContains(page, CARTON_BARCODE)
        from core.importer import run_import
        from core.registry import get_doctype

        content = "الصنف,الوحدة,المعامل (كم وحدة أساسية),الباركود\nPEP - بيبسي,درزن,12,\n".encode()
        UOM.objects.create(company=self.company, name="درزن")
        result = run_import(get_doctype("item-uom"), "u.csv", content, mode="insert", dry_run=False, user=self.owner)
        self.assertEqual(result["created"], 1, result["errors"])
