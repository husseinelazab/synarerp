"""عزل الشركات داخل PostgreSQL: قاعدة البيانات نفسها تمنع صفوف شركة أخرى ولو تجاوز الكود ترشيح التطبيق."""
import unittest

from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from core import rls
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


@unittest.skipUnless(connection.vendor == "postgresql", "عزل الصفوف خاص بـ PostgreSQL")
class RowLevelSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a, cls.owner_a = make_company("شركة أ للعزل", "rls_a")
        cls.b, cls.owner_b = make_company("شركة ب للعزل", "rls_b")
        with tenant_context(cls.b):
            cls.secret = Customer.objects.create(name="عميل سري لشركة ب")

    def setUp(self):
        # الاختبار كالخادم تماماً: بالدور المقيد (المالك والمدير يتجاوزان RLS)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            self.restore = cursor.fetchone()[0] != rls.role()
        if self.restore:
            rls.activate_role(connection)

    def tearDown(self):
        if self.restore:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")

    def test_every_tenant_table_has_the_policy(self):
        status = rls.status()
        self.assertGreater(status["tables"], 100)
        self.assertEqual(status["protected"], status["tables"])
        self.assertTrue(status["enforced"])

    def test_other_company_rows_invisible_even_without_app_filter(self):
        with tenant_context(self.a):
            self.assertFalse(Customer.all_objects.filter(pk=self.secret.pk).exists())
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM selling_customer WHERE company_id = %s", [self.b.pk])
                self.assertEqual(cursor.fetchone()[0], 0)
            self.assertEqual(Customer.all_objects.filter(company=self.b).update(name="عُدّل"), 0)
            self.assertEqual(Customer.all_objects.filter(company=self.b).delete()[0], 0)
        with tenant_context(self.b):
            self.assertEqual(Customer.objects.get(pk=self.secret.pk).name, "عميل سري لشركة ب")

    def test_writing_into_another_company_is_rejected(self):
        with tenant_context(self.a):
            with self.assertRaises(DatabaseError), transaction.atomic():
                Customer.all_objects.create(company=self.b, name="إدراج في شركة أخرى")
            with self.assertRaises(DatabaseError), transaction.atomic():
                Customer.all_objects.filter(company=self.a).update(company=self.b)

    def test_nested_context_switches_and_restores(self):
        with tenant_context(self.a):
            with tenant_context(self.b):
                self.assertTrue(Customer.all_objects.filter(pk=self.secret.pk).exists())
            self.assertFalse(Customer.all_objects.filter(pk=self.secret.pk).exists())
        # بلا شركة (مهام النظام والأوامر الإدارية): كل الشركات
        self.assertTrue(Customer.all_objects.filter(pk=self.secret.pk).exists())

    def test_failed_transaction_does_not_leak_context(self):
        with tenant_context(self.a):
            try:
                with transaction.atomic(), tenant_context(self.b):
                    raise RuntimeError("فشل داخل سياق شركة ب")
            except RuntimeError:
                pass
            self.assertFalse(Customer.all_objects.filter(pk=self.secret.pk).exists())

    def test_requests_of_a_company_user_are_confined(self):
        self.client.force_login(self.owner_a)
        page = self.client.get(f"/app/customer/{self.secret.pk}/")
        self.assertEqual(page.status_code, 404)
        self.assertNotContains(self.client.get("/search/", {"q": "سري"}), "عميل سري")
