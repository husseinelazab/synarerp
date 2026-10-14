import datetime
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from beneficiaries.models import (AidDisbursement, AidProgram, Beneficiary, BeneficiaryStatus, CaseAssessment,
                                  FamilyMember)
from beneficiaries.tests import make_nonprofit
from core.models import Attachment, Notification
from core.tenancy import tenant_context
from portal.demo import seed_demo
from portal.models import PortalLogin, ProfileUpdateRequest, ServiceRequest, ServiceStatus, UpdateStatus
from website.models import Website
from website.site import clear_cache

BASE = "/site/albirr/portal/"
OTHER = "/site/other/portal/"
TODAY = timezone.localdate()
PDF = b"%PDF-1.4\n%test\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


@override_settings(SYNAR_MESSAGING_ASYNC=False)
class PortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية البر للبوابة", "portal_owner")
        cls.other, cls.other_user = make_nonprofit("جمعية أخرى للبوابة", "portal_other")
        with tenant_context(cls.company):
            Website.objects.create(slug="albirr", is_published=True)
            cls.family = Beneficiary.objects.create(
                full_name="نورة سعد العتيبي", national_id="1100000001", phone="0551234567", city="الرياض",
                district="النسيم", status=BeneficiaryStatus.APPROVED, notes="ملاحظة داخلية سرية",
                last_score=Decimal("77.5"), next_review_date=TODAY + datetime.timedelta(days=100))
            FamilyMember.objects.create(parent=cls.family, full_name="سارة", relation="daughter", gender="female",
                                        national_id="1100000009", health_status="حالة صحية سرية")
            cls.neighbor = Beneficiary.objects.create(full_name="أم خالد", national_id="1100000002",
                                                      phone="0557654321", status=BeneficiaryStatus.APPROVED)
            program = AidProgram.objects.get(name="مساعدة نقدية")
            cls.mine = AidDisbursement.objects.create(beneficiary=cls.family, program=program, amount=Decimal("750"))
            cls.theirs = AidDisbursement.objects.create(beneficiary=cls.neighbor, program=program,
                                                        amount=Decimal("999"))
            cls.draft = AidDisbursement.objects.create(beneficiary=cls.family, program=program, amount=Decimal("333"))
            AidDisbursement.objects.filter(pk__in=[cls.mine.pk, cls.theirs.pk]).update(docstatus=1)
        with tenant_context(cls.other):
            Website.objects.create(slug="other", is_published=True)
            # نفس الهوية والجوال في جمعية أخرى
            cls.twin = Beneficiary.objects.create(full_name="ملف في جمعية أخرى", national_id="1100000001",
                                                  phone="0551234567", status=BeneficiaryStatus.APPROVED)

    def setUp(self):
        clear_cache()
        patcher = mock.patch("messaging.service.send", return_value=(None, ""))
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def send_code(self, base=BASE, national_id="1100000001", phone="0551234567", code=123456):
        with mock.patch("portal.site_views.secrets.randbelow", return_value=code):
            return self.client.post(base, {"action": "send", "national_id": national_id, "phone": phone,
                                           "website": ""})

    def login(self, base=BASE):
        self.send_code(base)
        response = self.client.post(base, {"action": "verify", "code": "123456", "website": ""})
        self.assertEqual(response.status_code, 302)
        return response

    # ─────────── الدخول ───────────

    def test_login_success_shows_file_without_internal_data(self):
        self.assertEqual(self.client.get(f"{BASE}home/").status_code, 302)
        response = self.send_code(phone="+966 55 123 4567")
        self.assertTrue(self.send.called)
        self.assertIn("123456", self.send.call_args.args[2])
        self.assertEqual(self.send.call_args.args[1], "0551234567")
        self.assertNotIn("123456", PortalLogin.all_objects.get().code_hash)
        sent_message = response.content.decode()
        response = self.client.post(BASE, {"action": "verify", "code": "123456", "website": ""})
        self.assertRedirects(response, f"{BASE}home/", fetch_redirect_response=False)
        page = self.client.get(f"{BASE}home/")
        self.assertContains(page, self.family.file_number)
        self.assertContains(page, "معتمد")
        self.assertContains(page, "سارة")
        for secret in ("ملاحظة داخلية سرية", "حالة صحية سرية", "77.5", "1100000009"):
            self.assertNotContains(page, secret)
        self.assertFalse(PortalLogin.all_objects.exists())
        self.assertIn("إن كانت البيانات مطابقة", sent_message)
        # الخروج
        self.client.post(f"{BASE}logout/")
        self.assertEqual(self.client.get(f"{BASE}home/").status_code, 302)

    def test_no_enumeration_on_wrong_phone_or_id(self):
        good = self.send_code().content.decode()
        self.client.cookies.clear()
        self.send.reset_mock()
        for national_id, phone in (("1100000001", "0500000000"), ("1999999999", "0551234567")):
            self.client.cookies.clear()
            response = self.send_code(national_id=national_id, phone=phone)
            self.assertContains(response, "إن كانت البيانات مطابقة")
            self.assertContains(response, "أدخل رمز التحقق")
            self.assertFalse(self.send.called)
            wrong = self.client.post(BASE, {"action": "verify", "code": "123456", "website": ""})
            self.assertContains(wrong, "رمز التحقق غير صحيح أو منتهي الصلاحية")
            self.assertEqual(self.client.get(f"{BASE}home/").status_code, 302)
        self.assertIn("إن كانت البيانات مطابقة", good)

    def test_otp_attempts_limited_and_expiry(self):
        self.send_code(code=111111)
        for _ in range(5):
            self.client.post(BASE, {"action": "verify", "code": "999999", "website": ""})
        response = self.client.post(BASE, {"action": "verify", "code": "111111", "website": ""})
        self.assertContains(response, "تجاوزت عدد المحاولات")
        # المحاولات محفوظة على الرمز نفسه أيضاً (لا يتجاوزها مسح الجلسة)
        self.assertEqual(PortalLogin.all_objects.get().attempts, 5)
        self.client.cookies.clear()
        self.client.post(BASE, {"action": "send", "national_id": "1100000001", "phone": "0551234567", "website": ""})
        response = self.client.post(BASE, {"action": "verify", "code": "111111", "website": ""})
        self.assertContains(response, "تجاوزت عدد المحاولات")

    def test_expired_code_rejected(self):
        self.send_code()
        PortalLogin.all_objects.update(sent_at=timezone.now() - datetime.timedelta(minutes=11))
        response = self.client.post(BASE, {"action": "verify", "code": "123456", "website": ""})
        self.assertContains(response, "رمز التحقق غير صحيح أو منتهي الصلاحية")

    def test_resend_wait_and_rate_limit(self):
        self.send_code()
        response = self.send_code()
        self.assertContains(response, "انتظر دقيقة")
        self.assertEqual(self.send.call_count, 1)

    def test_cross_company_isolation(self):
        self.login()
        # جلسة الجمعية الأولى لا تفتح بوابة الجمعية الأخرى
        self.assertEqual(self.client.get(f"{OTHER}home/").status_code, 302)
        # رمز جمعية لا يصلح لجمعية أخرى
        self.client.cookies.clear()
        self.send_code(base=OTHER, code=222222)
        response = self.client.post(BASE, {"action": "verify", "code": "222222", "website": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"{BASE}home/").status_code, 302)
        response = self.client.post(OTHER, {"action": "verify", "code": "222222", "website": ""})
        self.assertEqual(response.status_code, 302)
        page = self.client.get(f"{OTHER}home/")
        self.assertContains(page, "ملف في جمعية أخرى")
        self.assertNotContains(page, "نورة")

    def test_aid_list_scoped_to_beneficiary(self):
        self.login()
        page = self.client.get(f"{BASE}aid/")
        self.assertContains(page, "750")
        self.assertNotContains(page, "999")  # مساعدة مستفيد آخر
        self.assertNotContains(page, "333")  # مسودة لم تعتمد
        self.assertEqual(page.context["totals"]["cash"], Decimal("750"))

    def test_last_study_shows_only_result(self):
        with tenant_context(self.company):
            CaseAssessment.objects.create(beneficiary=self.family, findings="توصية داخلية للباحث")
            CaseAssessment.objects.filter(beneficiary=self.family).update(docstatus=1, is_eligible=True,
                                                                         score_percent=Decimal("81.5"))
        self.login()
        page = self.client.get(f"{BASE}home/")
        self.assertContains(page, "مستحق")
        self.assertNotContains(page, "توصية داخلية")
        self.assertNotContains(page, "81.5")

    # ─────────── تحديث البيانات ───────────

    def test_profile_update_request_and_staff_approval(self):
        self.login()
        response = self.client.post(f"{BASE}profile/", {"phone": "0559998887", "city": "جدة", "district": "الروضة",
                                                        "housing_type": "rented", "note": "انتقلنا", "website": ""})
        self.assertContains(response, "استلمنا طلب التحديث")
        self.family.refresh_from_db()
        self.assertEqual(self.family.phone, "0551234567")  # لا يتغير الملف قبل الاعتماد
        update = ProfileUpdateRequest.all_objects.get()
        self.assertEqual(update.beneficiary, self.family)
        self.assertEqual(update.status, UpdateStatus.PENDING)
        self.assertTrue(Notification.all_objects.filter(company=self.company, title__contains=update.number).exists())
        again = self.client.post(f"{BASE}profile/", {"phone": "0551111111", "website": ""})
        self.assertContains(again, "قيد المراجعة")
        self.assertEqual(ProfileUpdateRequest.all_objects.count(), 1)

        self.client.force_login(self.user)
        detail = self.client.get(f"/app/portal-profile-update/{update.pk}/")
        self.assertContains(detail, "جدة")
        self.client.post(f"/app/portal-profile-update/{update.pk}/approve/")
        update.refresh_from_db()
        self.family.refresh_from_db()
        self.assertEqual(update.status, UpdateStatus.APPROVED)
        self.assertEqual(update.decided_by, self.user)
        self.assertEqual((self.family.phone, self.family.city, self.family.district, self.family.housing_type),
                         ("0559998887", "جدة", "الروضة", "rented"))

    def test_staff_decline_keeps_file(self):
        with tenant_context(self.company):
            update = ProfileUpdateRequest.objects.create(beneficiary=self.family, city="مكة")
        self.client.force_login(self.user)
        self.client.post(f"/app/portal-profile-update/{update.pk}/decline/")
        update.refresh_from_db()
        self.family.refresh_from_db()
        self.assertEqual(update.status, UpdateStatus.REJECTED)
        self.assertEqual(self.family.city, "الرياض")

    # ─────────── المستندات ───────────

    def test_document_upload_validation(self):
        self.login()
        url = f"{BASE}documents/"
        fake = SimpleUploadedFile("report.pdf", b"<html>not a pdf</html>", content_type="application/pdf")
        self.assertContains(self.client.post(url, {"files": [fake]}), "لا يطابق نوعه")
        exe = SimpleUploadedFile("tool.exe", b"MZ....", content_type="application/octet-stream")
        self.assertContains(self.client.post(url, {"files": [exe]}), "الأنواع المسموحة")
        big = SimpleUploadedFile("big.pdf", PDF + b"0" * (5 * 1024 * 1024), content_type="application/pdf")
        self.assertContains(self.client.post(url, {"files": [big]}), "5 ميجابايت")
        many = [SimpleUploadedFile(f"f{i}.pdf", PDF, content_type="application/pdf") for i in range(11)]
        self.assertContains(self.client.post(url, {"files": many}), "الحد الأقصى 10")
        self.assertFalse(Attachment.all_objects.exists())

        response = self.client.post(url, {"files": [SimpleUploadedFile("lease.pdf", PDF, "application/pdf"),
                                                    SimpleUploadedFile("id.png", PNG, "text/plain")],
                                          "description": "عقد الإيجار"})
        self.assertContains(response, "تم رفع 2 ملف")
        files = Attachment.all_objects.filter(company=self.company, doctype="beneficiary", object_id=self.family.pk)
        self.assertEqual(files.count(), 2)
        self.assertEqual(files.get(file_name="id.png").content_type, "image/png")
        self.assertContains(self.client.get(url), "lease.pdf")
        # مرفقات الموظفين على الملف لا تظهر في البوابة
        with tenant_context(self.company):
            Attachment.objects.create(doctype="beneficiary", object_id=self.family.pk, file_name="internal.pdf",
                                      content=PDF, uploaded_by=self.user)
        self.assertNotContains(self.client.get(url), "internal.pdf")

    # ─────────── طلبات الخدمة ───────────

    def test_service_request_creation_and_reply(self):
        self.login()
        response = self.client.post(f"{BASE}requests/", {"kind": "restudy", "website": "",
                                                         "description": "تغيّر دخل الأسرة وأطلب إعادة الدراسة"})
        self.assertContains(response, "استلمنا طلبك")
        item = ServiceRequest.all_objects.get()
        self.assertEqual((item.beneficiary, item.company, item.status), (self.family, self.company, ServiceStatus.NEW))
        with tenant_context(self.company):
            ServiceRequest.objects.create(beneficiary=self.neighbor, kind="other", description="طلب جارتي الخاص")
        item.reply = "حُدد موعد زيارة الباحث يوم الأحد"
        item.save()
        page = self.client.get(f"{BASE}requests/")
        self.assertContains(page, "حُدد موعد زيارة الباحث")
        self.assertNotContains(page, "طلب جارتي الخاص")

        self.client.force_login(self.user)
        self.client.post(f"/app/portal-service-request/{item.pk}/complete/")
        item.refresh_from_db()
        self.assertEqual(item.status, ServiceStatus.DONE)

    def test_staff_lists_and_demo_seed(self):
        result = seed_demo(self.company)
        self.assertIsNotNone(result)
        seed_demo(self.company)
        self.assertEqual(ProfileUpdateRequest.all_objects.filter(company=self.company).count(), 1)
        self.assertEqual(ServiceRequest.all_objects.filter(company=self.company).count(), 1)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/app/portal-profile-update/").status_code, 200)
        self.assertContains(self.client.get("/app/portal-service-request/"), result[1].number)
