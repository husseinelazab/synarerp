from store.models import DonationOrder
from store.tests import BASE, StoreTestBase, paid_payment


class StorefrontTests(StoreTestBase):
    def pay(self, opportunity, amount, name="محمد الخيّر"):
        self.add(opportunity, now=True, custom_amount=str(amount))
        order = self.checkout(donor_name=name)
        self.callback(paid_payment(order, payment_id=f"pay_{order.pk}"))
        return order

    def test_home_search_kinds_and_zakat_banner(self):
        page = self.client.get(BASE)
        self.assertContains(page, "حاسبة الزكاة")
        self.assertContains(page, "?kind=zakat")
        found = self.client.get(BASE, {"q": "السقيا"})
        self.assertContains(found, "السقيا العامة")
        self.assertNotContains(found, "سهم وقف البئر")
        self.assertContains(self.client.get(BASE, {"sort": "popular"}), "السقيا العامة")

    def test_header_cart_count(self):
        self.add(self.general, amount="100")
        self.assertContains(self.client.get("/site/albirr/about/"), 'class="badge-count">1<')

    def test_recent_donations_hide_names_and_count_donors(self):
        self.pay(self.general, 150)
        page = self.client.get(f"{BASE}{self.general.pk}/")
        self.assertContains(page, "آخر التبرعات")
        self.assertContains(page, "فاعل خير")
        self.assertNotContains(page, "محمد الخيّر")
        self.assertContains(page, "<b>1</b><small>عملية</small>", html=False)

    def test_completed_opportunity_closed(self):
        self.general.target_amount = 100
        self.general.save()
        self.pay(self.general, 100)
        page = self.client.get(f"{BASE}{self.general.pk}/")
        self.assertContains(page, "اكتمل المبلغ المستهدف")
        count = DonationOrder.all_objects.filter(company=self.company).count()
        response = self.add(self.general, now=True, amount="50")
        self.assertContains(response, "اكتمل المبلغ المستهدف")
        self.assertEqual(self.client.session.get(f"store_cart_{self.company.pk}", []), [])
        self.assertEqual(DonationOrder.all_objects.filter(company=self.company).count(), count)

    def test_zakat_calculator_and_prefilled_amount(self):
        self.settings.gold_gram_price = 300
        self.settings.save()
        page = self.client.get(f"{BASE}zakat/")
        self.assertContains(page, "حاسبة الزكاة")
        self.assertContains(page, 'value="300.00"')
        self.assertContains(page, f'data-base="{BASE}{self.zakat.pk}/"')
        prefilled = self.client.get(f"{BASE}{self.zakat.pk}/", {"amount": "3657"})
        self.assertContains(prefilled, 'value="3657"')

    def test_thanks_page_share(self):
        order = self.pay(self.general, 200)
        self.assertContains(self.client.get(f"{BASE}thanks/{order.number}/"), "شارك الأجر")
