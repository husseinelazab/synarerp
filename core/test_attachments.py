from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import Attachment, Membership, Role
from core.tenancy import tenant_context
from saas.tests import make_company
from selling.models import Customer


class AttachmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("شركة المرفقات", "att_owner")
        cls.other, cls.other_owner = make_company("شركة أخرى", "att_other")
        with tenant_context(cls.company):
            cls.customer = Customer.objects.create(name="عميل بمرفق")

    def url(self, suffix=""):
        return f"/app/customer/{self.customer.pk}/attachments/{suffix}"

    def upload(self, name="عقد.pdf", content=b"%PDF-1.4 x"):
        return self.client.post(self.url(), {"files": SimpleUploadedFile(name, content, "application/pdf"),
                                             "description": "عقد التوريد"})

    def test_upload_list_download_delete(self):
        self.client.force_login(self.owner)
        self.upload()
        attachment = Attachment.all_objects.get()
        self.assertEqual((attachment.company, attachment.size, attachment.doctype), (self.company, 10, "customer"))
        detail = self.client.get(f"/app/customer/{self.customer.pk}/")
        self.assertContains(detail, "عقد.pdf")
        response = self.client.get(self.url(f"{attachment.pk}/"))
        self.assertEqual(response.content, b"%PDF-1.4 x")
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response["Content-Disposition"])
        inline = self.client.get(self.url(f"{attachment.pk}/?inline=1"))
        self.assertEqual(inline["Content-Type"], "application/pdf")
        self.client.post(self.url(f"{attachment.pk}/delete/"))
        self.assertFalse(Attachment.all_objects.exists())
        self.upload()
        self.client.post(f"/app/customer/{self.customer.pk}/delete/")
        self.assertFalse(Customer.all_objects.filter(pk=self.customer.pk).exists())
        self.assertFalse(Attachment.all_objects.exists())  # تُحذف مرفقات السجل المحذوف

    def test_size_limit_and_isolation(self):
        self.client.force_login(self.owner)
        self.upload("big.bin", b"0" * (10 * 1024 * 1024 + 1))
        self.assertFalse(Attachment.all_objects.exists())
        self.upload()
        attachment = Attachment.all_objects.get()
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(self.url(f"{attachment.pk}/")).status_code, 404)
        self.assertEqual(self.upload().status_code, 404)

    def test_read_only_user_downloads_but_cannot_change(self):
        self.client.force_login(self.owner)
        self.upload()
        attachment = Attachment.all_objects.get()
        viewer = User.objects.create_user("att_viewer", "v@example.com", "pass12345")
        Membership.objects.create(user=viewer, company=self.company, role=Role.USER, read_only_doctypes=["customer"])
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url(f"{attachment.pk}/")).status_code, 200)
        self.upload("second.pdf")
        self.client.post(self.url(f"{attachment.pk}/delete/"))
        self.assertEqual(Attachment.all_objects.count(), 1)
        blocked = User.objects.create_user("att_blocked", "b@example.com", "pass12345")
        Membership.objects.create(user=blocked, company=self.company, role=Role.USER, denied_doctypes=["customer"])
        self.client.force_login(blocked)
        self.assertNotEqual(self.client.get(self.url(f"{attachment.pk}/")).status_code, 200)
