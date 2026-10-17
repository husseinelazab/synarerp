import datetime
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from beneficiaries.tests import make_nonprofit
from beneficiaries.models import Beneficiary
from core.tenancy import tenant_context
from portal.models import ServiceSlot, ServiceBooking
from website.models import Website
from website.site import clear_cache


class AppointmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company,cls.owner=make_nonprofit("جمعية المواعيد","appointments_owner")
        cls.other,cls.foreign=make_nonprofit("جهة مواعيد أخرى","appointments_other")
        with tenant_context(cls.company):
            Website.objects.create(slug="appointments",is_published=True)
            cls.beneficiary=Beneficiary.objects.create(full_name="مستفيد موعد",national_id="1234567890",phone="0500000000")
            cls.second=Beneficiary.objects.create(full_name="مستفيد آخر",national_id="1234567891",phone="0500000001")
            cls.slot=ServiceSlot.objects.create(title="لقاء الباحث",starts_at=timezone.now()+datetime.timedelta(days=1),ends_at=timezone.now()+datetime.timedelta(days=1,hours=1),officer=cls.owner,published=True)
        with tenant_context(cls.other):
            cls.foreign_slot=ServiceSlot.objects.create(title="موعد جهة أخرى",starts_at=timezone.now()+datetime.timedelta(days=1),ends_at=timezone.now()+datetime.timedelta(days=1,hours=1),officer=cls.foreign,published=True)

    def setUp(self):
        clear_cache()
        session=self.client.session;session[f"portal_ben_{self.company.pk}"]=self.beneficiary.pk;session.save()
        self.url="/site/appointments/portal/appointments/"

    def test_capacity_duplicate_and_cancel_release_seat(self):
        self.assertEqual(self.client.post(self.url,{"slot":self.slot.pk}).status_code,302)
        self.assertEqual(self.client.post(self.url,{"slot":self.slot.pk}).status_code,302)
        self.assertEqual(ServiceBooking.objects.filter(slot=self.slot).count(),1)
        session=self.client.session;session[f"portal_ben_{self.company.pk}"]=self.second.pk;session.save()
        self.client.post(self.url,{"slot":self.slot.pk})
        self.assertEqual(ServiceBooking.objects.filter(slot=self.slot).count(),1)
        session=self.client.session;session[f"portal_ben_{self.company.pk}"]=self.beneficiary.pk;session.save()
        booking=ServiceBooking.objects.get(slot=self.slot)
        self.client.post(self.url,{"action":"cancel","booking":booking.pk});booking.refresh_from_db()
        self.assertEqual(booking.status,"cancelled")
        self.client.post(self.url,{"slot":self.slot.pk})
        self.assertEqual(ServiceBooking.objects.filter(slot=self.slot,status="pending").count(),1)

    def test_foreign_slot_and_own_bookings_only(self):
        self.assertEqual(self.client.post(self.url,{"slot":self.foreign_slot.pk}).status_code,404)
        with tenant_context(self.company):
            ServiceBooking.objects.create(slot=self.slot,beneficiary=self.second,note="ملاحظة مستفيد آخر")
        self.assertNotContains(self.client.get(self.url),"ملاحظة مستفيد آخر")

    def test_review_requires_reason_and_valid_state(self):
        self.client.post(self.url,{"slot":self.slot.pk})
        booking=ServiceBooking.objects.get(slot=self.slot)
        self.client.force_login(self.owner)
        url=reverse("service_booking_review",args=[booking.pk])
        self.client.post(url,{"decision":"rejected"});booking.refresh_from_db();self.assertEqual(booking.status,"pending")
        self.client.post(url,{"decision":"confirmed"});booking.refresh_from_db();self.assertEqual(booking.status,"confirmed")
        self.client.post(url,{"decision":"completed"});booking.refresh_from_db();self.assertEqual(booking.status,"confirmed")
        self.client.post(url,{"decision":"cancelled","reply":"تعارض موعد، يرجى إعادة الحجز"});booking.refresh_from_db();self.assertEqual(booking.status,"cancelled")
        self.client.force_login(self.foreign)
        self.assertEqual(self.client.post(url,{"decision":"confirmed"}).status_code,404)

    def test_staff_can_set_time_of_day_in_slot_form(self):
        self.client.force_login(self.owner)
        page=self.client.get("/app/service-slot/new/")
        self.assertEqual(page.context["form"].fields["starts_at"].widget.input_type,"datetime-local")
        start=timezone.localtime()+datetime.timedelta(days=3)
        start=start.replace(hour=10,minute=30,second=0,microsecond=0)
        end=start+datetime.timedelta(hours=1)
        response=self.client.post("/app/service-slot/new/",{"title":"موعد بوقت محدد","starts_at":start.strftime("%Y-%m-%dT%H:%M"),"ends_at":end.strftime("%Y-%m-%dT%H:%M"),"capacity":1,"officer":self.owner.pk,"published":"on"})
        self.assertEqual(response.status_code,302)
        slot=ServiceSlot.all_objects.get(title="موعد بوقت محدد")
        self.assertEqual(timezone.localtime(slot.starts_at).hour,10)
        self.assertEqual(timezone.localtime(slot.starts_at).minute,30)
