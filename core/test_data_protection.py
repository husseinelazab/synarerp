import io
import json
import os
import tempfile
import zipfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from core.models import AuditLog, Membership, Notification, Role
from core.tenancy import tenant_context
from messaging.models import MessagingSettings
from saas.backups import backup_status
from saas.tests import make_company
from selling.models import Customer


class SupportAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة العميل", "custowner")
        cls.admin = User.objects.create_superuser("platformadmin", "p@example.com", "pass12345")

    def enter(self):
        self.client.force_login(self.admin)
        return self.client.post(f"/platform/companies/{self.company.pk}/", {"action": "enter"})

    def test_support_entry_is_logged_once_and_owner_notified(self):
        self.enter()
        self.assertEqual(self.client.get("/", follow=True).status_code, 200)
        self.client.get("/app/customer/")
        entries = AuditLog.objects.filter(company=self.company, action="access")
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().user, self.admin)
        self.assertTrue(Notification.all_objects.filter(company=self.company, user=self.owner,
                                                        title__contains="الدعم الفني").exists())

    def test_member_access_is_not_logged(self):
        self.client.force_login(self.owner)
        self.client.get("/")
        self.assertFalse(AuditLog.objects.filter(action="access").exists())

    def test_company_can_block_support(self):
        self.company.allow_support_access = False
        self.company.save()
        self.enter()
        response = self.client.get("/")
        self.assertNotEqual(getattr(response.wsgi_request, "company", None), self.company)
        self.assertFalse(AuditLog.objects.filter(action="access").exists())
        response = self.client.post(f"/platform/companies/{self.company.pk}/", {"action": "enter"})
        self.assertNotEqual(self.client.session.get("company_id"), self.company.pk)


class CompanyExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة التصدير", "expowner")
        cls.other, _ = make_company("شركة أخرى", "otherexp")
        with tenant_context(cls.company):
            Customer.objects.create(name="عميل التصدير")
            cfg = MessagingSettings.for_company(cls.company)
            cfg.token_encrypted = "SECRET-VALUE"
            cfg.save()
        with tenant_context(cls.other):
            Customer.objects.create(name="عميل لا يجب أن يظهر")

    def download(self):
        self.client.force_login(self.owner)
        response = self.client.post("/settings/export-all/")
        self.assertEqual(response.status_code, 200)
        return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))

    def test_export_contains_only_company_data_without_secrets(self):
        zf = self.download()
        names = zf.namelist()
        self.assertIn("manifest.json", names)
        manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["company"], "شركة التصدير")
        everything = "".join(zf.read(n).decode() for n in names)
        self.assertIn("عميل التصدير", everything)
        self.assertNotIn("عميل لا يجب أن يظهر", everything)
        self.assertNotIn("SECRET-VALUE", everything)
        self.assertNotIn("pbkdf2", everything)  # كلمات مرور المستخدمين
        # أسطر المستندات (بلا حقل شركة) تُصدَّر عبر مستندها
        tables = {t["table"] for t in manifest["tables"]}
        self.assertIn("core.company", tables)
        self.assertIn("accounts.account", tables)
        self.assertIn("auth.user", tables)
        rows = json.loads(zf.read("data/selling.customer.json"))
        self.assertTrue(all(r["fields"]["company"] == self.company.pk for r in rows))

    def test_export_is_logged_and_owner_only(self):
        self.download()
        self.assertTrue(AuditLog.objects.filter(company=self.company, action="export").exists())
        manager = User.objects.create_user("expmanager", password="pass12345")
        Membership.objects.create(user=manager, company=self.company, role=Role.ADMIN)
        self.client.force_login(manager)
        self.assertEqual(self.client.get("/settings/export-all/").status_code, 200)
        response = self.client.post("/settings/export-all/")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get("Content-Type"), "application/zip")

    def test_support_mode_cannot_export(self):
        admin = User.objects.create_superuser("supportx", "s@example.com", "pass12345")
        self.client.force_login(admin)
        self.client.post(f"/platform/companies/{self.company.pk}/", {"action": "enter"})
        response = self.client.post("/settings/export-all/")
        self.assertNotEqual(response.get("Content-Type"), "application/zip")

    def test_management_command(self):
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.zip")
            call_command("export_company", str(self.company.pk), out=path, stdout=io.StringIO(), stderr=io.StringIO())
            with zipfile.ZipFile(path) as zf:
                self.assertIn("data/selling.customer.json", zf.namelist())


class BackupStatusTests(TestCase):
    def test_reports_missing_unencrypted_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(SYNAR_BACKUP_DIR=tmp):
            self.assertIn("لا توجد أي نسخة احتياطية بعد.", backup_status()["problems"])
            open(os.path.join(tmp, "synar-20260101-020000.sql.gz"), "wb").write(b"x")
            status = backup_status()
            self.assertEqual(status["count"], 1)
            self.assertTrue(any("غير مشفّرة" in p for p in status["problems"]))
            open(os.path.join(tmp, "synar-20260102-020000.sql.gz.gpg"), "wb").write(b"x")
            with open(os.path.join(tmp, "status.json"), "w") as fh:
                json.dump({"time": "2026-01-02T02:00:00+03:00", "ok": False, "message": "فشل فحص سلامة النسخة"}, fh)
            status = backup_status()
            self.assertTrue(status["latest"]["encrypted"])
            self.assertTrue(any("فشلت آخر محاولة" in p for p in status["problems"]))
        with override_settings(SYNAR_BACKUP_DIR="/nonexistent-dir"):
            self.assertFalse(backup_status()["available"])
