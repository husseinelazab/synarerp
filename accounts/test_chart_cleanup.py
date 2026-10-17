from django.test import TestCase
from core.tenancy import tenant_context
from saas.tests import make_company
from accounts.models import Account


class ChartCleanupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_company("دليل تجريبي","chart_owner")
        cls.other,cls.other_owner=make_company("دليل آخر","chart_other")
        with tenant_context(cls.company):
            cls.parent=Account.objects.create(name="دليل سابق",number="OLD",root_type="asset",is_group=True)
            cls.leaf=Account.objects.create(name="حساب سابق غير مستخدم",parent=cls.parent,root_type="asset")
        with tenant_context(cls.other):
            cls.foreign=Account.objects.create(name="حساب جهة أخرى",root_type="asset")

    def test_review_and_delete_unused_tree_keeps_linked_defaults_and_other_tenant(self):
        self.client.force_login(self.owner)
        url="/reports/accounts/chart-of-accounts/cleanup/"
        page=self.client.get(url)
        self.assertContains(page,self.leaf.name)
        response=self.client.post(url,{"preview":page.context["preview"],"confirm":"on"})
        self.assertEqual(response.status_code,302)
        self.assertFalse(Account.all_objects.filter(pk=self.leaf.pk).exists())
        self.assertFalse(Account.all_objects.filter(pk=self.parent.pk).exists())
        self.assertTrue(Account.all_objects.filter(pk=self.company.default_stock_account_id).exists())
        self.assertTrue(Account.all_objects.filter(pk=self.foreign.pk).exists())

    def test_signed_preview_cannot_be_used_for_another_company(self):
        self.client.force_login(self.owner)
        url="/reports/accounts/chart-of-accounts/cleanup/"
        token=self.client.get(url).context["preview"]
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.post(url,{"preview":token,"confirm":"on"}).status_code,403)
        self.assertTrue(Account.all_objects.filter(pk=self.leaf.pk).exists())
