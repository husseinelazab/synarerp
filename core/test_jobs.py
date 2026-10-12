import os
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from core.jobs import TASKS, enqueue, run_pending, task
from core.models import BackgroundJob, DataImport
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer

CALLS = []


@task("test_echo")
def echo(job=None, value=None):
    CALLS.append((job.company_id, value, Customer.objects.count()))


@task("test_fail")
def fail(job=None):
    raise RuntimeError("انكسر")


class JobTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_company("شركة المهام", "jobs")

    def setUp(self):
        CALLS.clear()

    def test_inline_when_disabled(self):
        job = enqueue("test_echo", company=self.company, value=1)
        self.assertEqual(job.status, "done")
        self.assertEqual(CALLS[0][:2], (self.company.pk, 1))

    @mock.patch.dict(os.environ, {"SYNAR_BACKGROUND_JOBS": "1"})
    def test_queued_then_run_in_tenant(self):
        with tenant_context(self.company):
            Customer.objects.create(name="ع")
        enqueue("test_echo", company=self.company, value=2)
        enqueue("test_fail", company=self.company)
        self.assertFalse(CALLS)
        self.assertEqual(run_pending(), 2)
        self.assertEqual(CALLS, [(self.company.pk, 2, 2)])  # مع العميل النقدي الافتراضي
        failed = BackgroundJob.objects.get(task="test_fail")
        self.assertEqual((failed.status, failed.error), ("failed", "انكسر"))
        self.assertIn("data_import", TASKS)

    @mock.patch.dict(os.environ, {"SYNAR_BACKGROUND_JOBS": "1"})
    def test_large_import_runs_in_background(self):
        from core import import_views

        rows = "اسم العميل\n" + "\n".join(f"عميل {i}" for i in range(5))
        self.client.force_login(self.user)
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post("/app/customer/import/", {
            "file": SimpleUploadedFile("c.csv", rows.encode()), "mode": "insert"})
        record = DataImport.objects.get()
        with mock.patch.object(import_views, "BACKGROUND_IMPORT_ROWS", 2):
            self.client.post(f"/app/customer/import/{record.pk}/")
        record.refresh_from_db()
        self.assertEqual(record.status, "queued", response)
        call_command("run_scheduler", "--once", stdout=open(os.devnull, "w"))
        record.refresh_from_db()
        self.assertEqual(record.status, "done")
        with tenant_context(self.company):
            self.assertEqual(Customer.objects.count(), 6)
