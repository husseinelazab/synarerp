import datetime
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.models import DocStatus, Membership, Notification, Role
from core.tenancy import tenant_context
from fleet.models import (FuelLog, MaintenanceRequest, MaintenanceStatus, TripStatus, Vehicle, VehicleRequest,
                          VehicleStatus)

TODAY = timezone.localdate()


class FleetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.manager = make_nonprofit("جمعية الحركة", "fleet_mgr")
        cls.staff = User.objects.create_user("fleet_staff", "s@example.com", "pass12345", first_name="سعد")
        Membership.objects.create(user=cls.staff, company=cls.company, role=Role.USER)

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.car = Vehicle.objects.create(plate_number="أ ب ج 1234", make="تويوتا", model="هايس",
                                          vehicle_type="bus", odometer=10000, last_service_km=8000)
        self.now = timezone.now().replace(microsecond=0)

    def trip(self, start_hours=1, hours=3, vehicle=None, **extra):
        start = self.now + datetime.timedelta(hours=start_hours)
        return VehicleRequest.objects.create(purpose="زيارة أسر مستفيدة", destination="حي النسيم", start_at=start,
                                             end_at=start + datetime.timedelta(hours=hours), vehicle=vehicle,
                                             created_by=self.staff, **extra)

    def test_approval_requires_free_vehicle_in_service(self):
        first = self.trip(vehicle=self.car)
        first.submit()
        self.assertEqual((first.status, first.start_odometer), (TripStatus.APPROVED, 10000))
        self.assertTrue(Notification.objects.filter(user=self.staff, title__contains="اعتُمد طلبك").exists())

        no_car = self.trip()
        with self.assertRaisesMessage(ValidationError, "حدد السيارة"):
            no_car.submit()
        clash = self.trip(start_hours=2, vehicle=self.car)
        with self.assertRaisesMessage(ValidationError, "محجوزة"):
            clash.submit()
        later = self.trip(start_hours=5, vehicle=self.car)
        self.car.status = VehicleStatus.MAINTENANCE
        self.car.save()
        with self.assertRaisesMessage(ValidationError, "في الصيانة"):
            later.submit()
        self.car.status = VehicleStatus.ACTIVE
        self.car.save()
        later.submit()
        self.assertEqual(later.status, TripStatus.APPROVED)

        bad = VehicleRequest(purpose="x", destination="y", start_at=self.now, end_at=self.now)
        with self.assertRaises(ValidationError):
            bad.clean()

    def test_reject_and_cancel(self):
        req = self.trip(vehicle=self.car)
        req.reject(self.manager, "لا توجد سيارة متاحة")
        self.assertEqual((req.docstatus, req.status), (DocStatus.CANCELLED, TripStatus.REJECTED))
        self.assertTrue(Notification.objects.filter(user=self.staff, title__contains="رُفض").exists())
        other = self.trip(vehicle=self.car)
        other.submit()
        other.cancel()
        self.assertEqual(other.status, TripStatus.CANCELLED)

    def test_complete_trip_updates_odometer(self):
        req = self.trip(vehicle=self.car)
        req.submit()
        with self.assertRaisesMessage(ValidationError, "أقل"):
            req.complete_trip(self.staff, 9000)
        req.complete_trip(self.staff, 10120)
        self.car.refresh_from_db()
        self.assertEqual((req.status, req.distance, self.car.odometer), (TripStatus.DONE, 120, 10120))
        with self.assertRaises(ValidationError):
            req.cancel()
        # المشوار المنفذ يبقى حاجزاً للفترة نفسها
        with self.assertRaisesMessage(ValidationError, "محجوزة"):
            self.trip(start_hours=2, vehicle=self.car).submit()

    def test_complete_trip_view(self):
        req = self.trip(vehicle=self.car)
        req.submit()
        self.client.force_login(self.staff)
        self.client.post(f"/fleet/trip/{req.pk}/complete/", {"end_odometer": "10050"})
        req.refresh_from_db()
        self.assertEqual(req.status, TripStatus.DONE)

    def test_maintenance_cycle_and_expense(self):
        self.assertTrue(self.car.service_due is False)
        self.car.odometer = 12600
        self.car.save()
        self.assertTrue(self.car.service_due)
        m = MaintenanceRequest(target="vehicle", category="mechanical", description="تغيير زيت")
        with self.assertRaises(ValidationError):
            MaintenanceRequest(target="facility", description="تسريب").full_clean()
        m.vehicle = self.car
        m.save()
        self.assertTrue(m.number.startswith(f"MNT-{TODAY.year}-"))
        m.start(self.manager)
        self.car.refresh_from_db()
        self.assertEqual((m.status, self.car.status), (MaintenanceStatus.IN_PROGRESS, VehicleStatus.MAINTENANCE))
        with self.assertRaises(ValidationError):
            m.record_expense(self.manager)
        m.cost, m.odometer_at_service, m.vendor = Decimal("450"), 12700, "ورشة الأمانة"
        m.save()
        m.finish(self.manager)
        self.car.refresh_from_db()
        self.assertEqual((m.status, m.completed_on), (MaintenanceStatus.DONE, TODAY))
        self.assertEqual((self.car.status, self.car.last_service_km, self.car.odometer),
                         (VehicleStatus.ACTIVE, 12700, 12700))
        self.assertFalse(self.car.service_due)
        self.assertEqual(m.available_actions, [("record_expense", "إثبات التكلفة محاسبياً")])
        entry = m.record_expense(self.manager)
        lines = {(line.account.number, line.debit, line.credit) for line in entry.accounts.select_related("account")}
        cash = self.company.default_cash_account.number
        self.assertEqual(lines, {("4120200401", Decimal("450"), 0), (cash, 0, Decimal("450"))})
        self.assertEqual(entry.docstatus, DocStatus.DRAFT)
        self.assertEqual(m.journal_entry, entry)
        self.assertEqual(m.available_actions, [])
        with self.assertRaises(ValidationError):
            m.record_expense(self.manager)

    def test_facility_maintenance_cancel_and_expense_account(self):
        m = MaintenanceRequest.objects.create(target="facility", facility="المقر — الدور الأول", category="plumbing",
                                              description="تسريب مياه", cost=200)
        m.start(self.manager)
        m.finish(self.manager)
        entry = m.record_expense(self.manager)
        self.assertTrue(entry.accounts.filter(account__number__startswith="41202002", debit=200).exists())
        other = MaintenanceRequest.objects.create(target="vehicle", vehicle=self.car, description="بطارية")
        other.start(self.manager)
        other.cancel(self.manager)
        self.car.refresh_from_db()
        self.assertEqual((other.status, self.car.status), (MaintenanceStatus.CANCELLED, VehicleStatus.ACTIVE))

    def test_fuel_log_updates_odometer_and_expense(self):
        log = FuelLog.objects.create(vehicle=self.car, liters=Decimal("60"), amount=Decimal("139.80"), odometer=10300)
        self.car.refresh_from_db()
        self.assertEqual(self.car.odometer, 10300)
        FuelLog.objects.create(vehicle=self.car, liters=10, amount=20, odometer=9000)
        self.car.refresh_from_db()
        self.assertEqual(self.car.odometer, 10300)
        entry = log.record_expense(self.manager)
        self.assertTrue(entry.accounts.filter(account__number="41201003", debit=Decimal("139.80")).exists())
        self.assertEqual(log.available_actions, [])

    def test_alerts_command(self):
        self.car.insurance_expiry = TODAY + datetime.timedelta(days=10)
        self.car.odometer = 13000
        self.car.save()
        urgent = MaintenanceRequest.objects.create(target="facility", facility="المستودع", category="electrical",
                                                   description="انقطاع كهرباء", priority="emergency")
        MaintenanceRequest.objects.filter(pk=urgent.pk).update(created_at=timezone.now() - datetime.timedelta(days=3))
        call_command("fleet_alerts", stdout=StringIO())
        titles = list(Notification.objects.filter(user=self.manager).values_list("title", flat=True))
        self.assertTrue(any("التأمين" in t for t in titles))
        self.assertTrue(any("الصيانة الدورية مستحقة" in t for t in titles))
        self.assertTrue(any("طارئة" in t for t in titles))
        count = Notification.objects.count()
        call_command("fleet_alerts", stdout=StringIO())
        self.assertEqual(Notification.objects.count(), count)

    def test_pages(self):
        req = self.trip(vehicle=self.car)
        req.submit()
        req.complete_trip(self.staff, 10080)
        FuelLog.objects.create(vehicle=self.car, liters=40, amount=93, odometer=10080)
        m = MaintenanceRequest.objects.create(target="facility", facility="المقر", category="hvac",
                                              description="صيانة مكيف", cost=300)
        m.start(self.manager)
        m.finish(self.manager)
        pending = self.trip(start_hours=30, vehicle=self.car)
        self.client.force_login(self.manager)
        response = self.client.get("/fleet/")
        self.assertContains(response, "لوحة الحركة والصيانة")
        self.assertContains(response, pending.name)
        response = self.client.get("/fleet/report/", {"from_date": TODAY.replace(day=1).isoformat(),
                                                      "to_date": (TODAY + datetime.timedelta(days=1)).isoformat()})
        self.assertContains(response, self.car.plate_number)
        self.assertContains(response, "تكييف")
        self.assertEqual(response.context["totals"]["km"], 80)
        objects = {"vehicle": self.car, "vehicle-request": req, "maintenance-request": m,
                   "fuel-log": FuelLog.objects.first()}
        for slug, obj in objects.items():
            for url in (f"/app/{slug}/", f"/app/{slug}/new/", f"/app/{slug}/{obj.pk}/"):
                self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_request_form_and_record_expense_action(self):
        self.client.force_login(self.staff)
        start = timezone.localtime(self.now + datetime.timedelta(days=2))
        response = self.client.post("/app/vehicle-request/new/", {
            "purpose": "توصيل سلال", "destination": "الحي الشرقي", "passengers": 2,
            "start_at": start.strftime("%Y-%m-%dT%H:%M"),
            "end_at": (start + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "posting_date": TODAY.isoformat()})
        req = VehicleRequest.objects.get(purpose="توصيل سلال")
        self.assertRedirects(response, req.get_absolute_url())
        self.assertEqual((req.created_by, req.status), (self.staff, TripStatus.PENDING))
        m = MaintenanceRequest.objects.create(target="vehicle", vehicle=self.car, description="إطارات", cost=800)
        m.start(self.manager)
        m.finish(self.manager)
        self.client.force_login(self.manager)
        response = self.client.post(f"/app/maintenance-request/{m.pk}/record_expense/")
        m.refresh_from_db()
        self.assertIsNotNone(m.journal_entry_id)
        self.assertRedirects(response, f"/app/journal-entry/{m.journal_entry_id}/edit/", fetch_redirect_response=False)
