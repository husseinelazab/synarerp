import importlib.util
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from core.models import ApiKey, Company, Membership, Notification, Role
from stock.barcode import ean_checksum
from stock.models import Bin, Item, Scale, ScaleSettings, StockCount, Warehouse


def scale_code(prefix, plu, value, plu_len=5, total_value_len=None):
    value_len = total_value_len or 12 - len(prefix) - plu_len
    body = prefix + str(plu).zfill(plu_len) + str(value).zfill(value_len)
    return body + ean_checksum(body)


def load_agent():
    spec = importlib.util.spec_from_file_location(
        "synar_scale_agent", Path(django_settings.BASE_DIR) / "tools" / "scale-agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScaleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", demo=True, tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.objects.get()
        cls.owner = User.objects.create_user("owner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        cls.mouse = Item.objects.get(code="MOU-001")  # نستخدمه صنفاً موزوناً: 75 للكيلو
        Item.objects.filter(pk=cls.mouse.pk).update(is_weighed=True, plu=42, shelf_life_days=5, scale_name="Mouse")
        cls.settings = ScaleSettings.objects.create(company=cls.company, enabled=True, prefixes="21,22")

    def setUp(self):
        cache.clear()
        self.client.force_login(self.owner)
        from pos.models import POSProfile

        POSProfile.objects.update(prices_include_tax=True)

    # ---------- صيغة الباركود ----------
    def test_parse_and_example(self):
        self.assertEqual(self.settings.value_length, 5)
        code = self.settings.example()
        self.assertEqual(code[:12], "210004201250")
        self.assertEqual(self.settings.parse(code), (42, 1250))
        self.assertEqual(self.settings.parse(scale_code("22", 42, 735)), (42, 735))
        self.assertIsNone(self.settings.parse(scale_code("23", 42, 735)))   # بادئة غير مسجلة
        self.assertIsNone(self.settings.parse(code[:12] + str((int(code[12]) + 1) % 10)))  # رقم تحقق خاطئ
        self.assertIsNone(self.settings.parse("6281234567895"))
        self.settings.enabled = False
        self.assertIsNone(self.settings.parse(code))

    def test_settings_validation(self):
        for bad in ({"prefixes": "2a"}, {"prefixes": "2,21"}, {"plu_length": 8}, {"prefixes": "21", "plu_length": 6}):
            s = ScaleSettings(company=self.company, **{"prefixes": "21", "plu_length": 5, **bad})
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                s.full_clean(exclude=["company"])

    def test_weighed_item_needs_plu(self):
        item = Item(company=self.company, code="X1", name="جبن", item_group=self.mouse.item_group, uom=self.mouse.uom,
                    is_weighed=True)
        with self.assertRaises(ValidationError):
            item.clean()
        item.plu = 1234567
        with self.assertRaises(ValidationError):
            item.clean()
        item.plu = 7
        item.clean()

    # ---------- القراءة في كل الشاشات ----------
    def test_resolve_weight_and_price_modes(self):
        item, qty = Item.resolve_code(scale_code("21", 42, 1250))
        self.assertEqual((item, qty), (self.mouse, Decimal("1.250")))
        self.assertEqual(Item.resolve_code("LAP-001")[1], None)  # الباركود العادي يبقى كما هو
        self.assertEqual(Item.resolve_code(scale_code("21", 999, 1250)), (None, None))
        ScaleSettings.objects.filter(pk=self.settings.pk).update(value_type="price")
        item, qty = Item.resolve_code(scale_code("21", 42, 9375))  # 93.75 ريال ÷ 75 = 1.25 كجم
        self.assertEqual(qty, Decimal("1.250"))

    def test_exact_barcode_wins_over_scale_format(self):
        laptop = Item.objects.get(code="LAP-001")
        code = scale_code("21", 42, 500)
        Item.objects.filter(pk=laptop.pk).update(barcode=code)
        self.assertEqual(Item.resolve_code(code), (laptop, None))

    def test_lookup_api_returns_weight_and_amount(self):
        data = self.client.get("/api/item/lookup/", {"code": scale_code("21", 42, 735)}).json()
        self.assertTrue(data["weighed"])
        self.assertEqual(Decimal(data["qty"]), Decimal("0.735"))
        self.assertEqual(Decimal(data["amount"]), Decimal("55.13"))  # 0.735 × 75

    def test_stock_count_scan_adds_weight(self):
        count = StockCount.objects.create(company=self.company, warehouse=Warehouse.objects.first())
        count.calculate_totals()
        count.save()
        count.scan(scale_code("21", 42, 1250))
        _, total = count.scan(scale_code("21", 42, 500), qty=2)  # عبوتان بنفس الوزن
        self.assertEqual(total, Decimal("2.250"))

    def test_pos_gets_scale_config_and_plu(self):
        from pos.models import POSProfile, POSShift
        from pos.services import checkout

        user = User.objects.create_user("cashier", password="pass12345")
        Membership.objects.create(user=user, company=self.company, role=Role.CASHIER)
        POSShift.open(POSProfile.objects.get(), user, opening_cash=0)
        self.client.force_login(user)
        page = self.client.get("/pos/").content.decode()
        self.assertIn('"scale": {"enabled": true, "prefixes": ["21", "22"], "pluLength": 5, "valueType": "weight"}', page)
        items = self.client.get("/pos/api/items/").json()["items"]
        self.assertEqual(next(i for i in items if i["id"] == self.mouse.pk)["plu"], 42)
        self.assertIsNone(next(i for i in items if i["code"] == "LAP-001")["plu"])
        inv = checkout(POSShift.current_for(user), user, [{"item": self.mouse.pk, "qty": 0.735}], paid_cash=100)
        self.assertEqual(inv.items.get().qty, Decimal("0.735"))
        self.assertEqual(inv.grand_total, Decimal("55.13"))

    # ---------- صفحة الموازين والتصدير ----------
    def test_scales_page_and_export(self):
        scale = Scale.objects.create(company=self.company, name="ميزان اللحوم", brand="CAS CL5200")
        page = self.client.get("/stock/scales/", {"test": scale_code("21", 42, 1250)})
        self.assertContains(page, "ميزان اللحوم")
        self.assertContains(page, "1.25</b>")
        self.assertContains(page, self.settings.example())
        csv_text = self.client.get(f"/stock/scales/export/{scale.pk}/").content.decode("utf-8-sig")
        self.assertIn("PLU,ItemCode,Name,ScaleName,UnitPrice", csv_text)
        self.assertIn("42,MOU-001,فأرة لاسلكية,Mouse,75.00,kg,5", csv_text)
        response = self.client.post("/stock/scales/", {"enabled": "on", "prefixes": "2", "plu_length": "6",
                                                       "value_type": "weight"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ScaleSettings.objects.get(company=self.company).value_length, 5)

    # ---------- واجهة البرنامج الوسيط ----------
    def api(self, method, path, raw, data=None, **headers):
        return getattr(self.client, method)(path, data=json.dumps(data) if data is not None else None,
                                            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}",
                                            **headers)

    def test_sync_api_versions_and_report(self):
        _, raw = ApiKey.issue(self.company, self.owner, "الموازين")
        scale = Scale.objects.create(company=self.company, name="ميزان 1")
        first = self.api("get", "/api/v1/scales/", raw)
        self.assertEqual(first.status_code, 200)
        data = first.json()
        self.assertEqual(data["barcode"], {"prefix": "21", "plu_length": 5, "value_type": "weight", "value_length": 5})
        plus = data["scales"][0]["plus"]
        self.assertEqual((plus[0]["plu"], plus[0]["price"], plus[0]["unit"]), (42, "75.00", "kg"))
        etag = first["ETag"]
        self.assertEqual(self.api("get", "/api/v1/scales/", raw, HTTP_IF_NONE_MATCH=etag).status_code, 304)
        Item.objects.filter(pk=self.mouse.pk).update(selling_rate=80)
        changed = self.api("get", "/api/v1/scales/", raw, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed["ETag"], etag)
        # البلاغ بالنتيجة، والتنبيه عند أول فشل فقط
        self.api("post", f"/api/v1/scales/{scale.pk}/report/", raw, {"ok": True, "message": "تم", "plu_count": 1,
                                                                      "version": "v1"})
        scale.refresh_from_db()
        self.assertEqual((scale.last_sync_ok, scale.last_plu_count, scale.last_version), (True, 1, "v1"))
        for _ in range(3):
            self.api("post", f"/api/v1/scales/{scale.pk}/report/", raw, {"ok": False, "message": "الميزان لا يرد"})
        self.assertEqual(Notification.objects.filter(title__contains="ميزان 1").count(), 1)
        self.assertEqual(self.client.get("/api/v1/scales/").status_code, 401)

    def test_sync_api_requires_stock_permission(self):
        clerk = User.objects.create_user("clerk", password="pass12345")
        Membership.objects.create(user=clerk, company=self.company, role=Role.USER, modules=["selling"])
        _, raw = ApiKey.issue(self.company, clerk, "مبيعات")
        self.assertEqual(self.api("get", "/api/v1/scales/", raw).status_code, 403)

    # ---------- البرنامج الوسيط نفسه ----------
    def test_agent_end_to_end(self):
        agent_mod = load_agent()
        _, raw = ApiKey.issue(self.company, self.owner, "الموازين")
        scale = Scale.objects.create(company=self.company, name="ميزان/الخضار")
        bad = Scale.objects.create(company=self.company, name="ميزان مباشر", driver="custom")
        laptop = Item.objects.get(code="LAP-001")
        test = self

        def http(method, path, body=None, headers=None):
            extra = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in (headers or {}).items()}
            response = test.api(method.lower(), path, raw, body, **extra)
            return response.status_code, {k: v for k, v in response.items()}, response.content

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "plu")
            state = os.path.join(tmp, "state.json")
            make = lambda: agent_mod.Agent("http://testserver", raw, state, {  # noqa: E731
                "file": agent_mod.FileDriver(out), "custom": agent_mod.CustomDriver()}, http=http)
            results = make().sync_once()
            self.assertEqual({name: ok for name, ok, _ in results}, {"ميزان/الخضار": True, "ميزان مباشر": False})
            folder = os.path.join(out, "ميزان_الخضار")
            with open(os.path.join(folder, "plu-all.csv"), encoding="utf-8-sig") as f:
                self.assertIn("42,MOU-001,فأرة لاسلكية,Mouse,75.00,kg,5", f.read())
            scale.refresh_from_db()
            bad.refresh_from_db()
            self.assertTrue(scale.last_sync_ok)
            self.assertFalse(bad.last_sync_ok)
            self.assertIn("بروتوكول", bad.last_sync_message)

            # الميزان المباشر يتوقف؛ بعدها لا تغيير → 304 ولا ملفات جديدة
            Scale.objects.filter(pk=bad.pk).update(enabled=False)
            make().sync_once()
            changes_before = sorted(p for p in os.listdir(folder) if p.startswith("plu-changes"))
            self.assertEqual(make().sync_once(), [])
            self.assertEqual(sorted(p for p in os.listdir(folder) if p.startswith("plu-changes")), changes_before)

            # تغيير سعر + صنف موزون جديد + حذف صنف: يُرسل الفرق فقط
            Item.objects.filter(pk=self.mouse.pk).update(selling_rate=80)
            Item.objects.filter(pk=laptop.pk).update(is_weighed=True, plu=7)
            make().sync_once()
            Item.objects.filter(pk=laptop.pk).update(is_weighed=False)
            make().sync_once()
            changes = sorted(p for p in os.listdir(folder) if p.startswith("plu-changes"))
            self.assertGreaterEqual(len(changes), len(changes_before) + 1)
            contents = ""
            for name in changes[len(changes_before):]:
                with open(os.path.join(folder, name), encoding="utf-8-sig") as f:
                    contents += f.read()
            self.assertIn("42,MOU-001,فأرة لاسلكية,Mouse,80.00,kg,5,,U", contents)
            self.assertIn("7,LAP-001", contents)
            self.assertIn(",D", contents)
            scale.refresh_from_db()
            self.assertEqual(scale.last_plu_count, 1)
