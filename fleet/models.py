"""إدارة الحركة والصيانة: سيارات الجمعية وطلبات المشاوير (عبر سلاسل الاعتماد) وصيانة السيارات والمباني والمرافق وسجل الوقود."""
import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import DocStatus, NamingSeries, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt

# حسابات الدليل الموحد للجهات غير الربحية (يُختار أول حساب فرعي تحت الرقم إن كان الرقم حساباً رئيسياً)
VEHICLE_MAINTENANCE_ACCOUNT = "41202004"   # صيانة وإصلاح - السيارات
BUILDING_MAINTENANCE_ACCOUNT = "41202002"  # صيانة وإصلاح - المباني
HVAC_MAINTENANCE_ACCOUNT = "41202011"      # صيانة وإصلاح - أجهزة التدفئة والتبريد والتهوية
FUEL_ACCOUNT = "41201003"                  # الوقود والمحروقات

EXPIRY_WARNING_DAYS = 30
SERVICE_WARNING_KM = 500


def expense_account(number):
    """الحساب الفرعي بالرقم المحدد، أو أول حساب فرعي تحته إن كان الرقم مجموعة."""
    from accounts.models import Account

    account = (Account.objects.filter(number=number, is_group=False, disabled=False).first()
               or Account.objects.filter(number__startswith=number, is_group=False, disabled=False)
               .order_by("number").first())
    if account is None:
        raise ValidationError(f"الحساب {number} غير موجود في دليل الحسابات (الدليل الموحد للجهات غير الربحية).")
    return account


def draft_expense_entry(company, user, date, account, amount, remarks):
    """مسودة قيد مصروف: من حساب المصروف إلى حساب النقدية الافتراضي، تُراجع ثم تُعتمد من المحاسبة."""
    from accounts.models import JournalEntry

    cash = company.require_account("default_cash_account")
    entry = JournalEntry.objects.create(posting_date=date, created_by=user, remarks=remarks)
    entry.accounts.create(account=account, debit=amount, description=remarks[:255])
    entry.accounts.create(account=cash, credit=amount, description=remarks[:255])
    entry.calculate_totals()
    entry.save()
    return entry


# ───────────────────────────── السيارات ─────────────────────────────

class VehicleType(models.TextChoices):
    SEDAN = "sedan", "سيارة صالون"
    FAMILY = "family", "عائلية"
    BUS = "bus", "حافلة"
    TRUCK = "truck", "نقل"
    OTHER = "other", "أخرى"


class VehicleStatus(models.TextChoices):
    ACTIVE = "active", "في الخدمة"
    MAINTENANCE = "maintenance", "في الصيانة"
    OUT = "out", "خارج الخدمة"


class Vehicle(TenantModel):
    """سيارة من أسطول الجمعية: وثائقها (الاستمارة والتأمين والفحص الدوري) وعداد المسافة وموعد الصيانة الدورية."""

    plate_number = models.CharField("رقم اللوحة", max_length=20)
    make = models.CharField("الشركة المصنعة", max_length=60, blank=True)
    model = models.CharField("الطراز", max_length=60, blank=True)
    year = models.PositiveSmallIntegerField("سنة الصنع", null=True, blank=True)
    vehicle_type = models.CharField("النوع", max_length=10, choices=VehicleType.choices, default=VehicleType.SEDAN)
    color = models.CharField("اللون", max_length=30, blank=True)
    driver = models.ForeignKey("hr.Employee", models.SET_NULL, null=True, blank=True, related_name="+",
                               verbose_name="السائق المسؤول")
    odometer = models.PositiveIntegerField("قراءة العداد (كم)", default=0)
    registration_expiry = models.DateField("انتهاء الاستمارة", null=True, blank=True)
    insurance_expiry = models.DateField("انتهاء التأمين", null=True, blank=True)
    inspection_expiry = models.DateField("انتهاء الفحص الدوري", null=True, blank=True)
    service_interval_km = models.PositiveIntegerField("الصيانة الدورية كل (كم)", default=5000)
    last_service_km = models.PositiveIntegerField("العداد عند آخر صيانة دورية", default=0)
    status = models.CharField("الحالة", max_length=12, choices=VehicleStatus.choices, default=VehicleStatus.ACTIVE)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "سيارة"
        verbose_name_plural = "السيارات"
        ordering = ["plate_number"]
        constraints = [tenant_unique("plate_number", model_label="vehicle")]

    def __str__(self):
        label = " ".join(p for p in (self.make, self.model) if p)
        return f"{self.plate_number} — {label}" if label else self.plate_number

    @property
    def next_service_km(self):
        return self.last_service_km + self.service_interval_km

    @property
    def service_due(self):
        return bool(self.service_interval_km) and self.odometer >= self.next_service_km - SERVICE_WARNING_KM

    @property
    def expiring_documents(self):
        """[(الوثيقة، تاريخ الانتهاء)] لما ينتهي خلال 30 يوماً أو انتهى فعلاً."""
        limit = timezone.localdate() + datetime.timedelta(days=EXPIRY_WARNING_DAYS)
        docs = [("الاستمارة", self.registration_expiry), ("التأمين", self.insurance_expiry),
                ("الفحص الدوري", self.inspection_expiry)]
        return [(label, date) for label, date in docs if date and date <= limit]

    @classmethod
    def expiring_q(cls, limit):
        return (Q(registration_expiry__lte=limit) | Q(insurance_expiry__lte=limit) | Q(inspection_expiry__lte=limit))

    def bump_odometer(self, reading):
        """يرفع العداد إن كانت القراءة الجديدة أعلى (لا ينقص العداد أبداً)."""
        if reading and reading > self.odometer:
            self.odometer = reading
            self.save(update_fields=["odometer"])


# ───────────────────────────── طلبات السيارات (المشاوير) ─────────────────────────────

class TripStatus(models.TextChoices):
    PENDING = "pending", "بانتظار الاعتماد"
    APPROVED = "approved", "معتمد"
    REJECTED = "rejected", "مرفوض"
    DONE = "done", "منفذ"
    CANCELLED = "cancelled", "ملغى"


class VehicleRequest(SubmittableDocument):
    """طلب سيارة لمشوار: يُقدَّم كمسودة، ويحدد المعتمد السيارة ثم يعتمده عبر سلاسل الاعتماد أو يرفضه."""

    naming_prefix = "TRP"
    posts_to_ledger = False
    posting_note = "الطلب معتمد؛ بعد العودة سجّل قراءة العداد من «إنهاء المشوار»."

    purpose = models.CharField("الغرض من المشوار", max_length=200)
    destination = models.CharField("الوجهة", max_length=200)
    start_at = models.DateTimeField("وقت الانطلاق")
    end_at = models.DateTimeField("وقت العودة المتوقع")
    passengers = models.PositiveSmallIntegerField("عدد الركاب", default=1)
    vehicle = models.ForeignKey(Vehicle, models.PROTECT, null=True, blank=True, related_name="trips",
                                verbose_name="السيارة", help_text="يحددها المعتمد قبل الاعتماد")
    status = models.CharField("حالة الطلب", max_length=10, choices=TripStatus.choices, default=TripStatus.PENDING,
                              editable=False)
    start_odometer = models.PositiveIntegerField("العداد عند الانطلاق", null=True, blank=True,
                                                 help_text="فارغ = قراءة عداد السيارة عند الاعتماد")
    end_odometer = models.PositiveIntegerField("العداد عند العودة", null=True, blank=True, editable=False)
    rejection_reason = models.CharField("سبب الرفض", max_length=255, blank=True, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "طلب سيارة"
        verbose_name_plural = "طلبات السيارات (المشاوير)"

    def __str__(self):
        return f"{self.name} — {self.destination}" if self.name else self.destination

    @property
    def is_rejected(self):
        return self.status == TripStatus.REJECTED

    @property
    def distance(self):
        if self.end_odometer is not None and self.start_odometer is not None:
            return self.end_odometer - self.start_odometer
        return 0

    def clean(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "وقت العودة يجب أن يكون بعد وقت الانطلاق."})

    def overlapping(self):
        """المشاوير المعتمدة أو المنفذة للسيارة نفسها التي تتقاطع مع فترة هذا الطلب."""
        return VehicleRequest.objects.filter(
            vehicle_id=self.vehicle_id, docstatus=DocStatus.SUBMITTED,
            status__in=[TripStatus.APPROVED, TripStatus.DONE],
            start_at__lt=self.end_at, end_at__gt=self.start_at,
        ).exclude(pk=self.pk)

    def validate_for_submit(self):
        self.clean()
        if self.vehicle_id is None:
            raise ValidationError("حدد السيارة قبل اعتماد الطلب (عدّل الطلب واختر السيارة).")
        if self.vehicle.status != VehicleStatus.ACTIVE:
            raise ValidationError(f"السيارة {self.vehicle} {self.vehicle.get_status_display()} ولا يمكن إسنادها لمشوار.")
        clash = self.overlapping().first()
        if clash is not None:
            raise ValidationError(f"السيارة محجوزة في الفترة نفسها للطلب {clash} "
                                  f"({timezone.localtime(clash.start_at):%Y-%m-%d %H:%M} – "
                                  f"{timezone.localtime(clash.end_at):%H:%M}).")
        if self.start_odometer is None:
            self.start_odometer = self.vehicle.odometer
        self.status = TripStatus.APPROVED

    def on_submit(self):
        self._notify_requester(f"اعتُمد طلبك للسيارة {self} — السيارة: {self.vehicle}")

    @property
    def draft_actions(self):
        return [("reject", "رفض الطلب")] if self.is_draft else []

    def reject(self, user, reason=""):
        if not self.is_draft:
            raise ValidationError("يمكن رفض الطلبات غير المعتمدة فقط.")
        self.rejection_reason = reason or f"رُفض بواسطة {user.get_full_name() or user.username}"
        self.docstatus, self.status = DocStatus.CANCELLED, TripStatus.REJECTED
        self.save(update_fields=["rejection_reason", "docstatus", "status"])
        self._notify_requester(f"رُفض طلبك للسيارة {self}: {self.rejection_reason}")

    def before_cancel(self):
        if self.status == TripStatus.DONE:
            raise ValidationError("لا يمكن إلغاء مشوار منفذ.")

    def on_cancel(self):
        self.status = TripStatus.CANCELLED
        self.save(update_fields=["status"])

    def complete_trip(self, user, end_odometer=None):
        """إنهاء المشوار بقراءة العداد عند العودة، ويُحدَّث عداد السيارة."""
        if not self.is_submitted or self.status != TripStatus.APPROVED:
            raise ValidationError("يمكن إنهاء المشاوير المعتمدة فقط.")
        if end_odometer is not None:
            self.end_odometer = end_odometer
        if self.end_odometer is None:
            raise ValidationError("أدخل قراءة العداد عند العودة.")
        start = self.start_odometer or 0
        if self.end_odometer < start:
            raise ValidationError(f"قراءة العداد عند العودة ({self.end_odometer}) أقل من قراءته عند الانطلاق ({start}).")
        self.status = TripStatus.DONE
        self.save(update_fields=["end_odometer", "status"])
        self.vehicle.bump_odometer(self.end_odometer)

    def _notify_requester(self, title):
        from core.notify import notify

        if self.created_by_id:
            notify(self.company, [self.created_by], title, self.get_absolute_url())


# ───────────────────────────── طلبات الصيانة ─────────────────────────────

class MaintenanceTarget(models.TextChoices):
    VEHICLE = "vehicle", "سيارة"
    FACILITY = "facility", "مبنى ومرافق"


class MaintenanceCategory(models.TextChoices):
    MECHANICAL = "mechanical", "ميكانيكا"
    ELECTRICAL = "electrical", "كهرباء"
    PLUMBING = "plumbing", "سباكة"
    HVAC = "hvac", "تكييف"
    CARPENTRY = "carpentry", "نجارة"
    PAINTING = "painting", "دهانات"
    OTHER = "other", "أخرى"


class MaintenancePriority(models.TextChoices):
    NORMAL = "normal", "عادية"
    URGENT = "urgent", "عاجلة"
    EMERGENCY = "emergency", "طارئة"


class MaintenanceStatus(models.TextChoices):
    NEW = "new", "جديد"
    IN_PROGRESS = "in_progress", "قيد التنفيذ"
    DONE = "done", "منجز"
    CANCELLED = "cancelled", "ملغى"


OPEN_MAINTENANCE = [MaintenanceStatus.NEW, MaintenanceStatus.IN_PROGRESS]


class MaintenanceRequest(TenantModel):
    """طلب صيانة لسيارة أو للمباني والمرافق: جديد ← قيد التنفيذ ← منجز، ثم إثبات تكلفته بمسودة قيد."""

    number = models.CharField("رقم الطلب", max_length=30, editable=False)
    date = models.DateField("التاريخ", default=timezone.localdate)
    target = models.CharField("الصيانة لـ", max_length=10, choices=MaintenanceTarget.choices,
                              default=MaintenanceTarget.VEHICLE)
    vehicle = models.ForeignKey(Vehicle, models.PROTECT, null=True, blank=True, related_name="maintenance",
                                verbose_name="السيارة")
    facility = models.CharField("المبنى / المرفق", max_length=150, blank=True, help_text="مثل: المقر الرئيسي — دورة مياه الدور الأول")
    category = models.CharField("التصنيف", max_length=12, choices=MaintenanceCategory.choices,
                                default=MaintenanceCategory.MECHANICAL)
    description = models.TextField("وصف العطل / العمل المطلوب")
    priority = models.CharField("الأولوية", max_length=10, choices=MaintenancePriority.choices,
                                default=MaintenancePriority.NORMAL)
    status = models.CharField("الحالة", max_length=12, choices=MaintenanceStatus.choices,
                              default=MaintenanceStatus.NEW, editable=False)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="المسؤول عن التنفيذ")
    vendor = models.CharField("المورد / الورشة", max_length=150, blank=True)
    cost = models.DecimalField("التكلفة", max_digits=14, decimal_places=2, default=0)
    completed_on = models.DateField("تاريخ الإنجاز", null=True, blank=True)
    odometer_at_service = models.PositiveIntegerField("العداد عند الصيانة", null=True, blank=True,
                                                      help_text="للسيارات: يُسجَّل كآخر صيانة دورية ويُحدَّث العداد")
    journal_entry = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True,
                                      related_name="+", editable=False, verbose_name="قيد المصروف")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                   related_name="+", editable=False, verbose_name="مقدم الطلب")
    created_at = models.DateTimeField("وقت الطلب", auto_now_add=True)

    class Meta:
        verbose_name = "طلب صيانة"
        verbose_name_plural = "طلبات الصيانة"
        ordering = ["-date", "-id"]
        constraints = [tenant_unique("number", model_label="maintenancerequest")]

    def __str__(self):
        subject = self.vehicle if self.target == MaintenanceTarget.VEHICLE and self.vehicle_id else self.facility
        return f"{self.number} — {subject}" if self.number else str(subject)

    def clean(self):
        if self.target == MaintenanceTarget.VEHICLE and not self.vehicle_id:
            raise ValidationError({"vehicle": "حدد السيارة."})
        if self.target == MaintenanceTarget.FACILITY and not self.facility.strip():
            raise ValidationError({"facility": "حدد المبنى أو المرفق."})
        if self.cost is not None and self.cost < 0:
            raise ValidationError({"cost": "التكلفة لا تكون سالبة."})

    def save(self, *args, **kwargs):
        if not self.number:
            from core.models import Company

            company = self.company if self.company_id else Company.current()
            self.number = NamingSeries.next_name(company, f"MNT-{self.date.year}")
        if self.created_by_id is None and not self.pk:
            from core.tenancy import get_current_user

            self.created_by = get_current_user()
        super().save(*args, **kwargs)

    @property
    def is_vehicle(self):
        return self.target == MaintenanceTarget.VEHICLE and self.vehicle_id is not None

    @property
    def is_open(self):
        return self.status in OPEN_MAINTENANCE

    @property
    def available_actions(self):
        if self.status == MaintenanceStatus.NEW:
            return [("start", "بدء التنفيذ"), ("cancel", "إلغاء الطلب")]
        if self.status == MaintenanceStatus.IN_PROGRESS:
            return [("finish", "تم الإنجاز"), ("cancel", "إلغاء الطلب")]
        if self.status == MaintenanceStatus.DONE and self.cost > 0 and self.journal_entry_id is None:
            return [("record_expense", "إثبات التكلفة محاسبياً")]
        return []

    def _release_vehicle(self):
        """تعود السيارة للخدمة إن لم يبقَ لها طلب صيانة آخر قيد التنفيذ."""
        if not self.is_vehicle:
            return
        vehicle = self.vehicle
        others = vehicle.maintenance.filter(status=MaintenanceStatus.IN_PROGRESS).exclude(pk=self.pk).exists()
        if vehicle.status == VehicleStatus.MAINTENANCE and not others:
            vehicle.status = VehicleStatus.ACTIVE
            vehicle.save(update_fields=["status"])

    def start(self, user):
        if self.status != MaintenanceStatus.NEW:
            raise ValidationError("يمكن بدء الطلبات الجديدة فقط.")
        self.status = MaintenanceStatus.IN_PROGRESS
        if self.assigned_to_id is None:
            self.assigned_to = user
        self.save(update_fields=["status", "assigned_to"])
        if self.is_vehicle and self.vehicle.status == VehicleStatus.ACTIVE:
            self.vehicle.status = VehicleStatus.MAINTENANCE
            self.vehicle.save(update_fields=["status"])

    def finish(self, user):
        if self.status != MaintenanceStatus.IN_PROGRESS:
            raise ValidationError("يمكن إنجاز الطلبات قيد التنفيذ فقط.")
        if self.cost is None or self.cost < 0:
            raise ValidationError("أدخل تكلفة الصيانة (صفر إن كانت بلا تكلفة).")
        self.status = MaintenanceStatus.DONE
        self.completed_on = self.completed_on or timezone.localdate()
        self.save(update_fields=["status", "completed_on"])
        if self.is_vehicle:
            vehicle = self.vehicle
            if self.odometer_at_service:
                vehicle.last_service_km = self.odometer_at_service
                vehicle.odometer = max(vehicle.odometer, self.odometer_at_service)
                vehicle.save(update_fields=["last_service_km", "odometer"])
            self._release_vehicle()
        if self.created_by_id and self.created_by_id != user.pk:
            from core.notify import notify

            notify(self.company, [self.created_by], f"أُنجز طلب الصيانة {self}", self.get_absolute_url())

    def cancel(self, user):
        if not self.is_open:
            raise ValidationError("الطلب منتهٍ بالفعل.")
        was_working = self.status == MaintenanceStatus.IN_PROGRESS
        self.status = MaintenanceStatus.CANCELLED
        self.save(update_fields=["status"])
        if was_working:
            self._release_vehicle()

    @property
    def expense_account_number(self):
        if self.target == MaintenanceTarget.VEHICLE:
            return VEHICLE_MAINTENANCE_ACCOUNT
        if self.category == MaintenanceCategory.HVAC:
            return HVAC_MAINTENANCE_ACCOUNT
        return BUILDING_MAINTENANCE_ACCOUNT

    def record_expense(self, user):
        """مسودة قيد بتكلفة الصيانة: مصروف الصيانة المناسب مقابل حساب النقدية الافتراضي."""
        if self.status != MaintenanceStatus.DONE:
            raise ValidationError("تُثبت التكلفة بعد إنجاز الصيانة.")
        if self.journal_entry_id is not None:
            raise ValidationError("أُثبتت تكلفة هذا الطلب من قبل.")
        if flt(self.cost) <= 0:
            raise ValidationError("لا توجد تكلفة لإثباتها.")
        entry = draft_expense_entry(self.company, user, self.completed_on or timezone.localdate(),
                                    expense_account(self.expense_account_number), self.cost,
                                    f"تكلفة صيانة {self} ({self.get_category_display()})"
                                    + (f" — {self.vendor}" if self.vendor else ""))
        self.journal_entry = entry
        self.save(update_fields=["journal_entry"])
        return entry


# ───────────────────────────── سجل الوقود ─────────────────────────────

class FuelLog(TenantModel):
    """تعبئة وقود لسيارة؛ قراءة العداد الأعلى تُحدِّث عداد السيارة."""

    vehicle = models.ForeignKey(Vehicle, models.PROTECT, related_name="fuel_logs", verbose_name="السيارة")
    date = models.DateField("التاريخ", default=timezone.localdate)
    liters = models.DecimalField("اللترات", max_digits=8, decimal_places=2, default=0)
    amount = models.DecimalField("المبلغ", max_digits=12, decimal_places=2, default=0)
    odometer = models.PositiveIntegerField("قراءة العداد", null=True, blank=True)
    station = models.CharField("المحطة", max_length=100, blank=True)
    journal_entry = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True,
                                      related_name="+", editable=False, verbose_name="قيد المصروف")

    class Meta:
        verbose_name = "تعبئة وقود"
        verbose_name_plural = "سجل الوقود"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.vehicle.plate_number} — {self.date:%Y-%m-%d}"

    def clean(self):
        if self.liters is not None and self.liters < 0 or self.amount is not None and self.amount < 0:
            raise ValidationError("اللترات والمبلغ لا تكون سالبة.")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.odometer:
            self.vehicle.bump_odometer(self.odometer)

    @property
    def available_actions(self):
        if self.amount > 0 and self.journal_entry_id is None:
            return [("record_expense", "إثبات التكلفة محاسبياً")]
        return []

    def record_expense(self, user):
        if self.journal_entry_id is not None:
            raise ValidationError("أُثبتت تكلفة هذه التعبئة من قبل.")
        if flt(self.amount) <= 0:
            raise ValidationError("لا يوجد مبلغ لإثباته.")
        entry = draft_expense_entry(self.company, user, self.date, expense_account(FUEL_ACCOUNT), self.amount,
                                    f"وقود السيارة {self.vehicle} ({self.liters} لتر){' — ' + self.station if self.station else ''}")
        self.journal_entry = entry
        self.save(update_fields=["journal_entry"])
        return entry
