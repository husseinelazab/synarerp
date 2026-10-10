import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from core.models import Company, Membership, Role
from stock import barcode
from stock.models import Bin, Item, StockCount, Warehouse


class BarcodeEncodingTests(TestCase):
    def test_ean_checksum_and_detection(self):
        self.assertEqual(barcode.ean_checksum("628123456789"), "5")
        self.assertTrue(barcode.is_valid_ean("6281234567895"))
        self.assertFalse(barcode.is_valid_ean("6281234567890"))
        self.assertEqual(barcode.encode("6281234567895")[0], "EAN-13")
        self.assertEqual(barcode.encode("96385074")[0], "EAN-8")
        self.assertEqual(barcode.encode("012345678905")[0], "UPC-A")
        self.assertEqual(barcode.encode("LAP-001")[0], "Code 128")

    def test_known_patterns(self):
        # EAN-13 للرقم 4006381333931: حارس البداية ثم أول رقم يساري (0) بترميز L
        self.assertTrue(barcode.encode("4006381333931")[1].startswith("101" + "0001101"))
        self.assertEqual(len(barcode.encode("4006381333931")[1]), 95)
        # Code 128: «LAP-001» بالمجموعة B: بداية + 7 رموز + تحقق + نهاية
        self.assertEqual(len(barcode.encode("LAP-001")[1]), 11 * 9 + 13)
        # الأرقام تُضغط بالمجموعة C: 12 رقماً = 6 رموز فقط
        self.assertEqual(len(barcode.encode("123456789012345")[1]) // 11, len(barcode._code128_values("123456789012345")) )
        self.assertIn("<svg", barcode.svg("6281234567895"))
        with self.assertRaises(barcode.BarcodeError):
            barcode.svg("صنف")

    def test_internal_codes_are_valid_and_unique(self):
        codes = {barcode.internal_ean13(i) for i in range(1, 500)}
        self.assertEqual(len(codes), 499)
        self.assertTrue(all(barcode.is_valid_ean(c) and c.startswith("200") for c in codes))


class BarcodeToolsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.clerk = User.objects.create_user("clerk", password="pass12345")
        Membership.objects.create(user=cls.clerk, company=cls.company, role=Role.USER, modules=["selling"])
        cls.mouse = Item.objects.get(code="MOU-001")
        cls.laptop = Item.objects.get(code="LAP-001")
        Item.objects.filter(pk=cls.mouse.pk).update(barcode="6281234567895")
        cls.warehouse = Warehouse.objects.first()

    def setUp(self):
        self.client.force_login(self.owner)

    def new_count(self, **kw):
        count = StockCount.objects.create(company=self.company, warehouse=self.warehouse, **kw)
        count.calculate_totals()
        count.save()
        return count

    def api(self, count, **body):
        return self.client.post(f"/stock/count/{count.pk}/scan/api/", json.dumps(body), content_type="application/json")

    def test_find_by_code_variants(self):
        self.assertEqual(Item.find_by_code("6281234567895"), self.mouse)
        self.assertEqual(Item.find_by_code(" lap-001 "), self.laptop)
        Item.objects.filter(pk=self.laptop.pk).update(barcode="012345678905")
        self.assertEqual(Item.find_by_code("0012345678905"), self.laptop)  # EAN-13 لباركود UPC-A
        self.assertIsNone(Item.find_by_code("nothing"))

    def test_scan_count_flow(self):
        count = self.new_count()
        system = Bin.objects.get(item=self.mouse, warehouse=self.warehouse).actual_qty
        self.assertEqual(self.api(count, code="6281234567895").json()["total"], 1)
        self.assertEqual(self.api(count, code="6281234567895", qty=4).json()["total"], 5)
        self.assertEqual(self.api(count, code="LAP-001", qty=2).json()["total"], 2)
        bad = self.api(count, code="000")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("لا يوجد صنف", bad.json()["error"])
        self.assertEqual(self.api(count, code="SRV-001").status_code, 400)  # خدمة ليست صنفاً مخزنياً
        undone = self.api(count, action="undo").json()
        self.assertEqual(undone["undone"]["qty"], 2)
        self.assertEqual(self.api(count, action="set", item=self.mouse.pk, qty=7).json()["total"], 7)
        self.assertEqual(self.api(count, action="set", item=self.mouse.pk, qty=-1).status_code, 400)
        totals = self.client.get(f"/stock/count/{count.pk}/scan/api/").json()
        self.assertEqual([(r["code"], r["qty"]) for r in totals["rows"]], [("MOU-001", 7.0)])
        # النقل للجرد: جزئي يُبقي غير الممسوح على رصيده الدفتري
        response = self.client.post(f"/stock/count/{count.pk}/scan/apply/")
        self.assertRedirects(response, f"/app/stock-count/{count.pk}/", fetch_redirect_response=False)
        row = count.items.get(item=self.mouse)
        self.assertEqual((row.counted_qty, row.difference_qty), (Decimal("7"), Decimal("7") - system))
        others = count.items.exclude(item=self.mouse)
        self.assertTrue(all(r.difference_qty == 0 for r in others))
        count.refresh_from_db()
        count.submit()
        self.assertEqual(Bin.objects.get(item=self.mouse, warehouse=self.warehouse).actual_qty, Decimal("7"))
        self.assertEqual(self.api(count, code="6281234567895").status_code, 400)  # معتمد

    def test_full_count_zeroes_unscanned(self):
        count = self.new_count()
        self.api(count, code="6281234567895", qty=3)
        count.apply_scans(zero_unscanned=True)
        counted = {r.item.code: r.counted_qty for r in count.items.select_related("item")}
        self.assertEqual(counted["MOU-001"], 3)
        self.assertTrue(all(q == 0 for code, q in counted.items() if code != "MOU-001"))

    def test_scan_respects_item_group(self):
        other_group = self.laptop.item_group
        count = self.new_count(item_group=other_group)
        if self.mouse.item_group_id != other_group.pk:
            with self.assertRaises(ValidationError):
                count.scan("6281234567895")
        count.scan("LAP-001")

    def test_lookup_api_and_price_including_vat(self):
        from pos.models import POSProfile

        POSProfile.objects.update(prices_include_tax=False)
        data = self.client.get("/api/item/lookup/", {"code": "6281234567895", "stock": 1}).json()
        self.assertEqual(data["name"], self.mouse.name)
        self.assertEqual(Decimal(data["price"]), (self.mouse.selling_rate * Decimal("1.15")).quantize(Decimal("0.01")))
        self.assertTrue(data["stock"])
        POSProfile.objects.update(prices_include_tax=True)
        data = self.client.get("/api/item/lookup/", {"code": "6281234567895"}).json()
        self.assertEqual(Decimal(data["price"]), self.mouse.selling_rate)
        self.assertEqual(self.client.get("/api/item/lookup/", {"code": "x"}).status_code, 404)
        # البحث متاح لمستخدم المبيعات (للمسح في الفواتير)، أما أدوات المخزون فلا
        self.client.force_login(self.clerk)
        self.assertEqual(self.client.get("/api/item/lookup/", {"code": "LAP-001"}).status_code, 200)
        self.assertNotEqual(self.client.get("/stock/labels/").status_code, 200)

    def test_generate_internal_barcodes(self):
        missing = Item.objects.filter(barcode="").count()
        self.assertGreater(missing, 0)
        self.client.post("/stock/labels/generate-barcodes/")
        self.assertFalse(Item.objects.filter(barcode="", disabled=False).exists())
        self.assertTrue(barcode.is_valid_ean(Item.objects.get(pk=self.laptop.pk).barcode))
        self.assertEqual(Item.objects.get(pk=self.mouse.pk).barcode, "6281234567895")  # لا يُغيَّر الموجود
        self.client.force_login(self.clerk)
        Item.objects.filter(pk=self.laptop.pk).update(barcode="")
        self.client.post("/stock/labels/generate-barcodes/")
        self.assertEqual(Item.objects.get(pk=self.laptop.pk).barcode, "")

    def test_print_labels_all_types_and_sizes(self):
        rows = json.dumps([
            {"id": self.mouse.pk, "copies": 3, "price": "86.25", "expiry": "2027-03-31", "batch": "B-7"},
            {"id": self.laptop.pk, "copies": 2},
            {"id": 999999, "copies": 5}, {"id": "x"},
        ])
        page = self.client.post("/stock/labels/", {"rows": rows, "type": "item", "size": "50x25", "show_code": "1",
                                                   "show_expiry": "1", "includes_tax": "1"})
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertEqual(html.count('class="lbl lbl-item"'), 5)
        self.assertIn("size: 50mm 25mm", html)
        self.assertIn("86.25", html)
        self.assertIn("2027-03-31", html)
        self.assertIn("#B-7", html)
        self.assertIn("6281234567895", html)
        for kind in ("shelf", "price", "barcode"):
            self.assertContains(self.client.post("/stock/labels/", {"rows": rows, "type": kind, "size": "100x50"}),
                                f"lbl-{kind}")
        sheet = self.client.post("/stock/labels/", {"rows": rows, "type": "item", "size": "a4-24", "start_at": "23"})
        html = sheet.content.decode()
        self.assertEqual(html.count('class="sheet"'), 2)  # يبدأ من الملصق 23 فيمتد لصفحة ثانية
        self.assertEqual(html.count('class="lbl lbl-item"'), 22 + 5)

    def test_labels_from_purchase_document(self):
        from buying.models import PurchaseInvoice

        pinv = PurchaseInvoice.objects.filter(docstatus=1, update_stock=True).first()
        row = pinv.items.first()
        row.batch_no, row.expiry_date = "LOT9", "2027-01-15"
        row.save()
        items = self.client.get("/stock/api/items/", {"doc": f"pinv-{pinv.pk}"}).json()["items"]
        first = next(i for i in items if i["id"] == row.item_id)
        self.assertEqual((first["batch"], first["expiry"], first["copies"]), ("LOT9", "2027-01-15", int(row.qty)))
        self.assertEqual(self.client.get("/stock/api/items/", {"doc": "pinv-99999"}).status_code, 404)
        self.assertContains(self.client.get(f"/app/purchase-invoice/{pinv.pk}/"), f"doc=pinv-{pinv.pk}")
