"""بوابة المستفيد: دخول رب الأسرة برمز تحقق، وطلبات تحديث البيانات والخدمة التي يعتمدها الموظفون من النظام."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from beneficiaries.models import HousingType
from core.models import NamingSeries, TenantModel
from core.tenancy import tenant_unique


class PortalLogin(TenantModel):
    """رمز دخول المستفيد للبوابة (مُجزّأ، بعدد محاولات ومدة صلاحية)."""

    beneficiary = models.ForeignKey("beneficiaries.Beneficiary", models.CASCADE, related_name="+")
    code_hash = models.CharField(max_length=64)
    sent_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "رمز دخول بوابة المستفيد"
        verbose_name_plural = "رموز دخول بوابة المستفيد"


def _number(obj, prefix):
    if not obj.number:
        from core.models import Company

        company = obj.company if obj.company_id else Company.current()
        obj.number = NamingSeries.next_name(company, prefix)


class UpdateStatus(models.TextChoices):
    PENDING = "pending", "بانتظار المراجعة"
    APPROVED = "approved", "معتمد"
    REJECTED = "rejected", "مرفوض"


class ProfileUpdateRequest(TenantModel):
    """طلب المستفيد تحديث بيانات التواصل والسكن؛ لا يُطبق على ملفه إلا باعتماد الموظف."""

    FIELDS = ("phone", "city", "district", "housing_type")

    number = models.CharField("رقم الطلب", max_length=30, editable=False)
    beneficiary = models.ForeignKey("beneficiaries.Beneficiary", models.CASCADE, related_name="portal_updates",
                                    verbose_name="المستفيد")
    phone = models.CharField("الجوال الجديد", max_length=20, blank=True)
    city = models.CharField("المدينة", max_length=60, blank=True)
    district = models.CharField("الحي", max_length=60, blank=True)
    housing_type = models.CharField("نوع السكن", max_length=10, blank=True,
                                    choices=HousingType.choices)
    note = models.TextField("ملاحظة المستفيد", blank=True, max_length=1000)
    status = models.CharField("الحالة", max_length=10, choices=UpdateStatus.choices, default=UpdateStatus.PENDING,
                              editable=False)
    reply = models.CharField("الرد للمستفيد", max_length=500, blank=True,
                             help_text="يظهر للمستفيد في البوابة (مثل سبب الرفض)")
    created_at = models.DateTimeField("تاريخ الطلب", default=timezone.now, editable=False)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                   editable=False, verbose_name="اعتمده")
    decided_at = models.DateTimeField("تاريخ القرار", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "طلب تحديث بيانات"
        verbose_name_plural = "طلبات تحديث البيانات"
        ordering = ["-id"]
        constraints = [tenant_unique("number", model_label="portal_profileupdate")]

    def __str__(self):
        return self.number or "طلب تحديث بيانات"

    def save(self, *args, **kwargs):
        _number(self, "PUR")
        super().save(*args, **kwargs)

    def changes(self):
        """[(الحقل، القيمة الحالية، القيمة المطلوبة)] للحقول المطلوب تغييرها فقط."""
        from beneficiaries.models import Beneficiary

        rows = []
        for name in self.FIELDS:
            new = getattr(self, name)
            old = getattr(self.beneficiary, name)
            if new and new != old:
                if name == "housing_type":
                    labels = dict(HousingType.choices)
                    old, new = labels.get(old, old), labels.get(new, new)
                rows.append((Beneficiary._meta.get_field(name).verbose_name, old, new))
        return rows

    @property
    def available_actions(self):
        if self.status == UpdateStatus.PENDING:
            return [("approve", "اعتماد وتطبيق على ملف المستفيد"), ("decline", "رفض الطلب")]
        return []

    def _decide(self, user, status):
        self.status, self.decided_by, self.decided_at = status, user, timezone.now()
        self.save(update_fields=["status", "decided_by", "decided_at"])

    def approve(self, user):
        if self.status != UpdateStatus.PENDING:
            raise ValidationError("تم البت في الطلب مسبقاً.")
        beneficiary = self.beneficiary
        updated = []
        for name in self.FIELDS:
            value = getattr(self, name)
            if value and value != getattr(beneficiary, name):
                setattr(beneficiary, name, value)
                updated.append(name)
        if updated:
            beneficiary.save(update_fields=updated)
        self._decide(user, UpdateStatus.APPROVED)

    def decline(self, user):
        if self.status != UpdateStatus.PENDING:
            raise ValidationError("تم البت في الطلب مسبقاً.")
        self._decide(user, UpdateStatus.REJECTED)


class ServiceKind(models.TextChoices):
    RESTUDY = "restudy", "إعادة دراسة الحالة"
    URGENT = "urgent", "مساعدة عاجلة"
    OTHER = "other", "طلب آخر"


class ServiceStatus(models.TextChoices):
    NEW = "new", "جديد"
    IN_PROGRESS = "in_progress", "قيد المعالجة"
    DONE = "done", "مكتمل"
    REJECTED = "rejected", "مرفوض"


class ServiceRequest(TenantModel):
    """طلب خدمة من المستفيد عبر البوابة (مساعدة جديدة أو إعادة دراسة)، يرد عليه الموظف ويظهر الرد للمستفيد."""

    number = models.CharField("رقم الطلب", max_length=30, editable=False)
    beneficiary = models.ForeignKey("beneficiaries.Beneficiary", models.CASCADE, related_name="portal_requests",
                                    verbose_name="المستفيد")
    kind = models.CharField("نوع الطلب", max_length=12, choices=ServiceKind.choices, default=ServiceKind.OTHER)
    description = models.TextField("تفاصيل الطلب", max_length=2000)
    status = models.CharField("الحالة", max_length=12, choices=ServiceStatus.choices, default=ServiceStatus.NEW,
                              editable=False)
    reply = models.TextField("الرد للمستفيد", blank=True, max_length=1000,
                             help_text="يظهر للمستفيد في البوابة؛ لا تكتب فيه ملاحظات داخلية")
    created_at = models.DateTimeField("تاريخ الطلب", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        verbose_name = "طلب خدمة من البوابة"
        verbose_name_plural = "طلبات الخدمة من البوابة"
        ordering = ["-id"]
        constraints = [tenant_unique("number", model_label="portal_servicerequest")]

    def __str__(self):
        return f"{self.number} — {self.get_kind_display()}" if self.number else self.get_kind_display()

    def save(self, *args, **kwargs):
        _number(self, "PSR")
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status in (ServiceStatus.NEW, ServiceStatus.IN_PROGRESS)

    @property
    def available_actions(self):
        actions = []
        if self.status == ServiceStatus.NEW:
            actions.append(("start", "بدء المعالجة"))
        if self.is_open:
            actions += [("complete", "تم التنفيذ"), ("decline", "رفض الطلب")]
        return actions

    def _set(self, status):
        self.status = status
        self.save(update_fields=["status", "updated_at"])

    def start(self, user):
        self._set(ServiceStatus.IN_PROGRESS)

    def complete(self, user):
        self._set(ServiceStatus.DONE)

    def decline(self, user):
        self._set(ServiceStatus.REJECTED)


class ServiceSlot(TenantModel):
    title = models.CharField("خدمة الموعد", max_length=150)
    location = models.CharField("المكان / رابط اللقاء", max_length=200, blank=True)
    starts_at = models.DateTimeField("البداية")
    ends_at = models.DateTimeField("النهاية")
    capacity = models.PositiveSmallIntegerField("المقاعد", default=1)
    officer = models.ForeignKey(settings.AUTH_USER_MODEL, models.PROTECT, related_name="+", verbose_name="الموظف المسؤول")
    published = models.BooleanField("متاح للحجز في البوابة", default=False)

    class Meta:
        verbose_name = "موعد خدمة متاح"
        verbose_name_plural = "المواعيد المتاحة للمستفيدين"
        ordering = ["starts_at","pk"]

    def __str__(self):
        return f"{self.title} — {self.starts_at:%Y/%m/%d %H:%M}"

    def clean(self):
        from core.models import Company, Membership
        company_id=self.company_id or getattr(Company.current(),"pk",None)
        if self.starts_at and self.ends_at and self.ends_at<=self.starts_at:
            raise ValidationError({"ends_at":"النهاية يجب أن تكون بعد البداية."})
        if not self.capacity:
            raise ValidationError({"capacity":"حدد مقعدًا واحدًا على الأقل."})
        if self.pk and self.capacity < self.bookings.filter(status__in=["pending","confirmed"]).count():
            raise ValidationError({"capacity":"لا يمكن تقليل المقاعد عن الحجوزات الحالية."})
        if self.officer_id and not Membership.objects.filter(company_id=company_id,user_id=self.officer_id,is_active=True,user__is_active=True).exists():
            raise ValidationError({"officer":"المسؤول ليس مستخدمًا نشطًا في الجهة."})

    def save(self,*args,**kwargs):
        from django.db import transaction
        from core.models import Company
        if not self.company_id:
            self.company=Company.current()
        with transaction.atomic():
            if self.pk:
                ServiceSlot.objects.select_for_update().get(pk=self.pk)
            self.full_clean()
            return super().save(*args,**kwargs)


class ServiceBooking(TenantModel):
    slot = models.ForeignKey(ServiceSlot, models.PROTECT, related_name="bookings", verbose_name="الموعد")
    beneficiary = models.ForeignKey("beneficiaries.Beneficiary", models.PROTECT, related_name="service_bookings", verbose_name="المستفيد")
    status = models.CharField("الحالة", max_length=12, choices=[("pending","بانتظار التأكيد"),("confirmed","مؤكد"),("completed","تم اللقاء"),("rejected","مرفوض"),("cancelled","ملغى")], default="pending")
    note = models.CharField("ملاحظة المستفيد", max_length=500, blank=True)
    reply = models.CharField("رد ظاهر للمستفيد", max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.PROTECT, null=True, related_name="+")

    class Meta:
        verbose_name = "حجز خدمة"
        verbose_name_plural = "حجوزات الخدمات"
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["company","slot","beneficiary"],condition=models.Q(status__in=["pending","confirmed"]),name="active_service_booking_once")]

    def __str__(self):
        return f"حجز {self.pk} — {self.slot.title}"
