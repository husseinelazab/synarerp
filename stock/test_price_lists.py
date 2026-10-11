from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from core.models import Company, Membership, Role
from pos.models import POSProfile, POSShift
from pos.services import checkout
from selling.models import Customer
from stock.models import UOM, Item, ItemGroup, ItemUOM, PriceList


class PriceListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_erp", tax_id="310122393500003", verbosity=0, stdout=open("/dev/null", "w"))
        cls.company = Company.current()
        cls.owner = User.objects.create_user("plowner", password="pass12345")
        Membership.objects.create(user=cls.owner, company=cls.company, role=Role.OWNER)
        group = ItemGroup.objects.first()
        cls.piece, cls.carton = UOM.objects.get(name="حبة"), UOM.objects.get(name="كرتون")
        cls.water = Item.objects.create(code="PL1", name="ماء", item_group=group, uom=cls.piece, selling_rate=2,
                                        is_stock_item=False)
        ItemUOM.objects.create(item=cls.water, uom=cls.carton, factor=24, selling_rate=45)
        cls.juice = Item.objects.create(code="PL2", name="عصير", item_group=group, uom=cls.piece, selling_rate=5,
                                        is_stock_item=False)
        cls.wholesale = PriceList.objects.create(name="الجملة")
        cls.wholesale.prices.create(item=cls.water, rate=Decimal("1.50"))
        cls.customer = Customer.objects.create(name="سوبرماركت", price_list=cls.wholesale)

    def test_rates_with_and_without_list(self):
        self.assertEqual(self.water.price_for(None, "selling", self.wholesale), Decimal("1.50"))
        self.assertEqual(self.water.price_for(self.carton.pk, "selling", self.wholesale), Decimal("36.00"))  # 1.5 × 24
        self.wholesale.prices.create(item=self.water, uom=self.carton, rate=34)
        self.assertEqual(self.water.price_for(self.carton.pk, "selling", self.wholesale), Decimal("34"))
        self.assertEqual(self.juice.price_for(None, "selling", self.wholesale), Decimal("5"))      # غير مدرج
        self.assertEqual(self.water.price_for(self.carton.pk, "selling"), Decimal("45"))          # بلا قائمة
        self.assertEqual(self.water.price_for(None, "buying", self.wholesale), Decimal("0"))      # الشراء لا يتأثر
        self.wholesale.disabled = True
        self.assertEqual(self.water.price_for(None, "selling", self.wholesale), Decimal("2"))

    def test_item_api_uses_customer_list(self):
        self.client.force_login(self.owner)
        data = self.client.get(f"/api/item/{self.water.pk}/", {"customer": self.customer.pk}).json()
        self.assertEqual(Decimal(data["selling_rate"]), Decimal("1.50"))
        self.assertEqual(Decimal(data["uoms"][str(self.carton.pk)]["selling_rate"]), Decimal("36.00"))
        self.assertEqual(data["price_list"], "الجملة")
        plain = self.client.get(f"/api/item/{self.water.pk}/").json()
        self.assertEqual(Decimal(plain["selling_rate"]), Decimal("2"))
        self.assertEqual(Decimal(plain["uoms"][str(self.carton.pk)]["selling_rate"]), Decimal("45"))

    def test_price_list_form(self):
        self.client.force_login(self.owner)
        response = self.client.post("/app/price-list/new/", {
            "name": "كبار العملاء", "notes": "",
            "prices-TOTAL_FORMS": 1, "prices-INITIAL_FORMS": 0, "prices-MIN_NUM_FORMS": 0, "prices-MAX_NUM_FORMS": 1000,
            "prices-0-item": self.juice.pk, "prices-0-uom": "", "prices-0-rate": "4.25",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.juice.price_for(None, "selling", PriceList.objects.get(name="كبار العملاء")),
                         Decimal("4.25"))

    def test_pos_profile_price_list(self):
        cashier = User.objects.create_user("plcashier", password="pass12345")
        Membership.objects.create(user=cashier, company=self.company, role=Role.CASHIER)
        profile = POSProfile.objects.get()
        profile.price_list = self.wholesale
        profile.save()
        shift = POSShift.open(profile, cashier, opening_cash=0)
        invoice = checkout(shift, cashier, [{"item": self.water.id, "qty": 2}, {"item": self.water.id, "qty": 1,
                                                                                "uom": self.carton.pk}],
                           paid_cash=100)
        self.assertEqual([row.rate for row in invoice.items.order_by("id")], [Decimal("1.50"), Decimal("36.00")])
        self.client.force_login(cashier)
        items = {i["code"]: i for i in self.client.get("/pos/api/items/").json()["items"]}
        self.assertEqual(items["PL1"]["rate"], 1.5)
        self.assertEqual(items["PL1"]["units"][0]["rate"], 36.0)
        self.assertEqual(items["PL2"]["rate"], 5.0)
