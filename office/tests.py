import datetime
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from beneficiaries.tests import make_nonprofit
from core.models import Membership, Notification, Role
from core.tenancy import tenant_context
from office.models import (AdminRequest, AdminRequestType, ArchiveCategory, ArchiveDocument, Circular,
                           Correspondence, CorrespondenceStatus)

TODAY = datetime.date.today()


class OfficeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.manager = make_nonprofit("جمعية المكتب", "office_mgr")
        cls.clerk = User.objects.create_user("office_clerk", "c@example.com", "pass12345", first_name="سارة")
        cls.other = User.objects.create_user("office_other", "o@example.com", "pass12345")
        for user in (cls.clerk, cls.other):
            Membership.objects.create(user=user, company=cls.company, role=Role.USER)

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_numbering_routing_and_closing(self):
        incoming = Correspondence.objects.create(direction="incoming", subject="طلب دعم", party="أمانة المنطقة")
        outgoing = Correspondence.objects.create(direction="outgoing", subject="رد على طلب", reply_to=incoming)
        self.assertEqual(incoming.number, f"IN-{TODAY.year}-00001")
        self.assertEqual(outgoing.number, f"OUT-{TODAY.year}-00001")
        incoming.route(self.manager, self.clerk, "للإفادة", TODAY + datetime.timedelta(days=3))
        self.assertEqual((incoming.assigned_to, incoming.status), (self.clerk, CorrespondenceStatus.IN_PROGRESS))
        self.assertTrue(Notification.objects.filter(user=self.clerk, title__contains="أُحيلت إليك").exists())
        incoming.close(self.manager)
        self.assertEqual(incoming.routes.count(), 2)
        self.assertEqual(incoming.replies.get(), outgoing)

    def test_confidential_visibility(self):
        secret = Correspondence.objects.create(direction="incoming", subject="سري للغاية", is_confidential=True)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/app/correspondence/{secret.pk}/").status_code, 404)
        self.assertNotContains(self.client.get("/app/correspondence/"), "سري للغاية")
        self.assertEqual(self.client.get(f"/office/documents/correspondence/{secret.pk}/letter/").status_code, 404)
        secret.route(self.manager, self.other)
        self.assertEqual(self.client.get(f"/app/correspondence/{secret.pk}/").status_code, 200)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(f"/app/correspondence/{secret.pk}/").status_code, 200)

    def test_route_view_and_letter(self):
        item = Correspondence.objects.create(direction="outgoing", subject="خطاب شكر", party="شركة الداعم",
                                             body="نشكركم على دعمكم.", signatory=self.manager)
        self.client.force_login(self.manager)
        self.client.post(f"/office/documents/correspondence/{item.pk}/route/", {"to_user": self.clerk.pk,
                                                                               "note": "للمتابعة"})
        item.refresh_from_db()
        self.assertEqual(item.assigned_to, self.clerk)
        outsider = User.objects.create_user("outsider", "x@example.com", "pass12345")
        self.client.post(f"/office/documents/correspondence/{item.pk}/route/", {"to_user": outsider.pk})
        item.refresh_from_db()
        self.assertEqual(item.assigned_to, self.clerk)  # لا إحالة لمستخدم من خارج الجهة
        letter = self.client.get(f"/office/documents/correspondence/{item.pk}/letter/")
        self.assertContains(letter, "نشكركم على دعمكم.")
        self.assertContains(letter, item.number)

    def test_admin_request_goes_through_approval(self):
        request_type = AdminRequestType.objects.get(name="طلب شراء")
        self.client.force_login(self.clerk)
        response = self.client.post("/app/admin-request/new/", {"request_type": request_type.pk,
                                                                 "subject": "طابعة للمكتب", "amount": "1500",
                                                                 "posting_date": TODAY.isoformat()})
        self.assertEqual(response.status_code, 302)
        request = AdminRequest.objects.get()
        self.client.post(f"/app/admin-request/{request.pk}/submit/")
        request.refresh_from_db()
        self.assertTrue(request.is_draft)  # بانتظار المدير
        self.assertTrue(Notification.objects.filter(user=self.manager, title__contains="بانتظار اعتمادك").exists())
        self.client.force_login(self.manager)
        self.client.post(f"/app/admin-request/{request.pk}/submit/")
        request.refresh_from_db()
        self.assertTrue(request.is_submitted)
        request.mark_done(self.manager)
        self.assertTrue(Notification.objects.filter(user=self.clerk, title__contains="تم تنفيذ").exists())

    def test_circulars_desk_dashboard_and_alerts(self):
        circular = Circular.objects.create(title="مواعيد الدوام في رمضان", body="من 10 إلى 3")
        Correspondence.objects.create(direction="incoming", subject="متأخرة", due_date=TODAY - datetime.timedelta(days=2))
        late = Correspondence.objects.get(subject="متأخرة")
        late.route(self.manager, self.clerk)
        ArchiveDocument.objects.create(title="ترخيص الجمعية", category=ArchiveCategory.objects.get(name="التراخيص والصكوك"),
                                       expiry_date=TODAY + datetime.timedelta(days=10))
        self.client.force_login(self.clerk)
        desk = self.client.get("/office/").content.decode()
        self.assertIn("مواعيد الدوام في رمضان", desk)
        self.assertIn("متأخرة", desk)
        self.client.post(f"/office/circular/{circular.pk}/ack/")
        self.assertEqual(circular.ack_count, 1)
        self.assertEqual(self.client.get("/office/documents/").status_code, 200)
        call_command("office_alerts", stdout=StringIO())
        self.assertTrue(Notification.objects.filter(user=self.clerk, title__contains="معاملة متأخرة").exists())
        self.assertTrue(Notification.objects.filter(title__contains="ترخيص الجمعية").exists())
        html = self.client.get("/").content.decode()
        self.assertIn("المكتب الإلكتروني", html)
        self.assertIn("إدارة التوثيق والمستندات", html)
        for url in ("/app/correspondence/new/?direction=outgoing", "/app/archive-document/new/",
                    f"/app/circular/{circular.pk}/", "/app/admin-request/new/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
