import datetime
import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.tenancy import tenant_context
from pos.models import POSProfile, POSShift
from pricing.engine import points_balance
from pricing.models import ItemPrice, LoyaltyProgram, PriceList, PricingRule
from saas.tests import make_company
from selling.models import Customer, CustomerGroup, SalesInvoice, SalesOrder
from stock.models import UOM, Item, ItemGroup, ItemUOM, Warehouse


class PricingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة التسعير", "pricer")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.group = ItemGroup.objects.first()
        self.item = Item.objects.create(code="P1", name="عصير", item_group=self.group, uom=UOM.objects.first(),
                                        is_stock_item=False, selling_rate=10, vat_category="Z",
                                        vat_exemption_reason="VATEX-SA-32")
        self.gift = Item.objects.create(code="G1", name="كوب هدية", item_group=self.group, uom=UOM.objects.first(),
                                        is_stock_item=False, selling_rate=5, vat_category="Z",
                                        vat_exemption_reason="VATEX-SA-32")
        self.customer = Customer.objects.create(name="عميل التسعير")
        self.today = datetime.date.today()

    def invoice(self, qty=1, customer=None, submit=False, **extra):
        inv = SalesInvoice.objects.create(customer=customer or self.customer, tax_rate=0, vat_category="Z",
                                          vat_exemption_reason="VATEX-SA-32", update_stock=False, **extra)
        inv.items.create(item=self.item, qty=qty, rate=0)
        inv.calculate_totals()
        inv.save()
        if submit:
            inv.submit()
        return inv

    def test_item_card_rate_fills_blank_rate(self):
        inv = self.invoice(3)
        row = inv.items.get()
        self.assertEqual(row.rate, Decimal("10"))
        self.assertEqual(inv.total, Decimal("30"))

    def test_price_list_by_customer_group_and_customer(self):
        wholesale = PriceList.objects.create(name="جملة")
        ItemPrice.objects.create(price_list=wholesale, item=self.item, rate=8)
        vip = PriceList.objects.create(name="كبار العملاء")
        ItemPrice.objects.create(price_list=vip, item=self.item, rate=7)
        self.customer.customer_group = CustomerGroup.objects.create(name="تجار الجملة", price_list=wholesale)
        self.customer.save()
        self.assertEqual(self.invoice().items.get().rate, Decimal("8"))
        self.customer.price_list = vip
        self.customer.save()
        self.assertEqual(self.invoice().items.get().rate, Decimal("7"))
        # قائمة المستند تتقدم على قائمة العميل
        self.assertEqual(self.invoice(price_list=wholesale).items.get().rate, Decimal("8"))

    def test_carton_price_from_list_uses_factor(self):
        carton = UOM.objects.create(name="كرتون12")
        ItemUOM.objects.create(item=self.item, uom=carton, factor=12)
        retail = PriceList.objects.create(name="تجزئة", is_default=True)
        ItemPrice.objects.create(price_list=retail, item=self.item, rate=9)
        inv = SalesInvoice.objects.create(customer=self.customer, tax_rate=0, vat_category="Z",
                                          vat_exemption_reason="VATEX-SA-32", update_stock=False)
        inv.items.create(item=self.item, qty=1, rate=0, uom=carton)
        inv.calculate_totals()
        self.assertEqual(inv.items.get().rate, Decimal("108"))

    def test_quantity_discount_and_reset_when_qty_drops(self):
        PricingRule.objects.create(name="خصم الكمية 10%", item=self.item, min_qty=10, discount_percentage=10)
        inv = self.invoice(12)
        row = inv.items.get()
        self.assertEqual((row.rate, row.price_list_rate, row.discount_percentage), (Decimal("9"), 10, 10))
        self.assertIsNotNone(row.pricing_rule)
        row.qty = 5
        row.save()
        inv.calculate_totals()
        row.refresh_from_db()
        self.assertEqual(row.rate, Decimal("10"))  # زالت القاعدة فعاد سعر القائمة
        self.assertIsNone(row.pricing_rule)

    def test_manual_rate_kept_without_rule(self):
        inv = self.invoice()
        row = inv.items.get()
        row.rate = 6
        row.save()
        inv.calculate_totals()
        row.refresh_from_db()
        self.assertEqual(row.rate, Decimal("6"))

    def test_best_rule_by_priority_and_specificity(self):
        PricingRule.objects.create(name="كل الأصناف 5%", apply_on="all", discount_percentage=5)
        PricingRule.objects.create(name="المجموعة 20%", apply_on="item_group", item_group=self.group,
                                   discount_percentage=20)
        self.assertEqual(self.invoice().items.get().pricing_rule.name, "المجموعة 20%")
        PricingRule.objects.create(name="سعر خاص للعميل", item=self.item, customer=self.customer,
                                   discount_type="rate", rate=Decimal("7.50"), priority=5)
        row = self.invoice().items.get()
        self.assertEqual((row.pricing_rule.name, row.rate), ("سعر خاص للعميل", Decimal("7.50")))
        other = Customer.objects.create(name="عميل آخر")
        self.assertEqual(self.invoice(customer=other).items.get().pricing_rule.name, "المجموعة 20%")

    def test_validity_dates(self):
        PricingRule.objects.create(name="عرض منتهٍ", item=self.item, discount_percentage=50,
                                   valid_upto=self.today - datetime.timedelta(days=1))
        self.assertIsNone(self.invoice().items.get().pricing_rule)

    def test_buy_two_get_one_free(self):
        PricingRule.objects.create(name="اشترِ 2 واحصل على كوب", item=self.item, min_qty=2, discount_type="none",
                                   free_item=self.gift, free_qty=1, recurse=True)
        inv = self.invoice(5)
        free = inv.items.get(is_free_item=True)
        self.assertEqual((free.item, free.qty, free.rate), (self.gift, Decimal("2"), 0))
        self.assertEqual(inv.total, Decimal("50"))
        inv.calculate_totals()  # إعادة الحساب لا تكرر الهدية
        self.assertEqual(inv.items.filter(is_free_item=True).count(), 1)
        inv.submit()

    def test_coupon_required_and_usage_limit(self):
        PricingRule.objects.create(name="كوبون رمضان", item=self.item, discount_percentage=25, coupon_code="ramadan",
                                   max_uses=1)
        self.assertIsNone(self.invoice().items.get().pricing_rule)
        inv = self.invoice(coupon_code="RAMADAN", submit=True)
        self.assertEqual(inv.items.get().rate, Decimal("7.50"))
        rule = PricingRule.objects.get()
        self.assertEqual(rule.used_count, 1)
        # استُنفد الكوبون: الفاتورة التالية بنفس الكود ترفض
        with self.assertRaises(ValidationError):
            self.invoice(coupon_code="RAMADAN", submit=True)
        inv.cancel()
        rule.refresh_from_db()
        self.assertEqual(rule.used_count, 0)

    def test_invalid_coupon_rejected(self):
        with self.assertRaises(ValidationError):
            self.invoice(coupon_code="NOPE", submit=True)

    def test_order_rates_kept_on_invoice(self):
        order = SalesOrder.objects.create(customer=self.customer, tax_rate=0, warehouse=Warehouse.objects.first())
        order.items.create(item=self.item, qty=2, rate=0)
        order.calculate_totals()
        order.save()
        order.submit()
        PricingRule.objects.create(name="عرض لاحق", item=self.item, discount_percentage=50)
        inv = order.make_invoice()
        inv.vat_category, inv.vat_exemption_reason = "Z", "VATEX-SA-32"
        inv.calculate_totals()
        self.assertEqual(inv.items.get().rate, Decimal("10"))  # سعر الأمر المتفق عليه

    def test_loyalty_earn_and_redeem(self):
        program = LoyaltyProgram.objects.create(name="نقاطي", amount_per_point=10, point_value=Decimal("0.5"))
        self.customer.loyalty_program = program
        self.customer.save()
        self.invoice(25, submit=True)  # 250 ريال = 25 نقطة
        self.assertEqual(points_balance(self.customer), 25)
        inv = self.invoice(2, loyalty_points=20)  # 20 ريال - 10 ريال قيمة النقاط
        self.assertEqual(inv.loyalty_amount, Decimal("10"))
        inv.submit()
        inv.refresh_from_db()
        self.assertEqual(inv.outstanding_amount, Decimal("10"))
        self.assertEqual(inv.loyalty_points_earned, 1)  # 10 ريال مدفوعة = نقطة
        self.assertEqual(points_balance(self.customer), 25 - 20 + 1)
        with self.assertRaises(ValidationError):
            self.invoice(2, loyalty_points=500, submit=True)
        inv.cancel()
        self.assertEqual(points_balance(self.customer), 25)

    def test_pos_quote_and_checkout_apply_promotions(self):
        PricingRule.objects.create(name="عرض الكاشير", item=self.item, min_qty=3, discount_percentage=10,
                                   channel="pos")
        profile = POSProfile.objects.first()
        profile.tax_rate = 0
        profile.save()
        shift = POSShift.objects.create(profile=profile, user=self.user)
        self.client.force_login(self.user)
        cart = {"items": [{"item": self.item.pk, "qty": 3}]}
        quote = self.client.post("/pos/api/quote/", json.dumps(cart), content_type="application/json").json()
        self.assertEqual(quote["due"], 27.0)
        self.assertEqual(quote["lines"][0]["rule"], "عرض الكاشير")
        self.assertFalse(SalesInvoice.objects.exists())  # التسعير لا يحفظ شيئاً
        res = self.client.post("/pos/api/checkout/", json.dumps({**cart, "paid_cash": 27}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        inv = SalesInvoice.objects.get()
        self.assertEqual(inv.grand_total, Decimal("27"))
        self.assertEqual(inv.pos_shift, shift)
        # العرض لقناة نقاط البيع فقط
        self.assertIsNone(self.invoice(3).items.get().pricing_rule)

    def test_item_api_returns_priced_rate(self):
        PricingRule.objects.create(name="خصم", item=self.item, discount_percentage=20)
        self.client.force_login(self.user)
        data = self.client.get(f"/api/item/{self.item.pk}/?kind=selling&customer={self.customer.pk}&qty=1").json()
        self.assertEqual(Decimal(data["priced"]["rate"]), Decimal("8"))
        self.assertEqual(data["priced"]["rule"], "خصم")

    def test_pages_render(self):
        self.client.force_login(self.user)
        for slug in ("price-list", "pricing-rule", "customer-group", "loyalty-program", "item-price"):
            self.assertEqual(self.client.get(f"/app/{slug}/").status_code, 200, slug)
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200, slug)
        self.assertEqual(self.client.get(f"/app/customer/{self.customer.pk}/").status_code, 200)
