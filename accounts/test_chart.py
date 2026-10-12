from django.test import TestCase

from accounts.chart import apply_unified_chart
from accounts.models import Account
from core.setup_data import CHART_OF_ACCOUNTS
from core.tenancy import tenant_context
from saas.tests import make_company


def total(nodes):
    return sum(1 + total(n[3] or []) for n in nodes)


class UnifiedChartTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة الدليل", "chart")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_new_company_gets_full_unified_chart(self):
        self.assertEqual(Account.objects.count(), total(CHART_OF_ACCOUNTS))
        self.assertEqual(Account.objects.get(number="1303").cash_flow_category, "investing")
        self.assertTrue(Account.objects.get(number="53").is_group)
        self.assertEqual(apply_unified_chart(self.company), (0, []))  # لا تكرار

    def test_existing_company_upgrade_keeps_custom_accounts(self):
        Account.objects.filter(number__in=["5301", "5302", "5303", "5304"]).delete()
        Account.objects.filter(number="53").delete()
        rent = Account.objects.get(number="5202")
        rent.name = "إيجار المعارض"
        rent.save()
        created, skipped = apply_unified_chart(self.company)
        self.assertEqual((created, skipped), (5, []))
        self.assertEqual(Account.objects.get(number="5202").name, "إيجار المعارض")

    def test_add_sub_account_from_chart(self):
        self.client.force_login(self.user)
        banks = Account.objects.get(number="11")
        page = self.client.get(f"/app/account/new/?parent={banks.pk}")
        self.assertContains(page, 'value="1117"')  # الرقم التالي
        response = self.client.post(f"/app/account/new/?parent={banks.pk}", {
            "parent": banks.pk, "number": "1117", "name": "بنك الراجحي - حساب الفرع", "account_type": "bank"})
        account = Account.objects.get(number="1117")
        self.assertEqual(account.root_type, "asset")  # من الأب
        self.assertRedirects(response, f"/app/account/{account.pk}/", fetch_redirect_response=False)
        # حساب تحت حساب فرعي جديد بالترقيم المتدرج
        group = Account.objects.create(number="1118", name="بنوك الفروع", parent=banks, is_group=True)
        self.assertContains(self.client.get(f"/app/account/new/?parent={group.pk}"), 'value="111801"')
        dup = self.client.post("/app/account/new/", {"parent": banks.pk, "number": "1117", "name": "مكرر"})
        self.assertContains(dup, "رقم الحساب مستخدم")
        chart = self.client.get("/reports/accounts/chart-of-accounts/")
        self.assertContains(chart, "+ فرعي")
        self.assertEqual(chart.context["unified_missing"], 0)
