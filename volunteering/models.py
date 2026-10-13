"""إدارة التطوع: المتطوعون، الفرص (بتسجيل عام برابط)، المشاركات وساعاتها، الشهادات، وقيمة التطوع محاسبياً."""
import secrets
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from beneficiaries.models import Gender, validate_national_id
from core.models import TenantModel
from core.tenancy import tenant_unique
from core.utils import flt

VOLUNTEER_EXPENSE_ACCOUNT = "42203001"  # مصروفات برامج وأنشطة غير مقيدة - خدمات تطوعية
VOLUNTEER_INCOME_ACCOUNT = "31203001"   # تبرعات وهبات غير مقيدة - خدمات تطوعية


class VolunteerStatus(models.TextChoices):
    NEW = "new", "جديد"
    APPROVED = "approved", "معتمد"
    INACTIVE = "inactive", "غير نشط"


class Volunteer(TenantModel):
    full_name = models.CharField("الاسم", max_length=150)
    national_id = models.CharField("رقم الهوية", max_length=10, validators=[validate_national_id])
    gender = models.CharField("الجنس", max_length=6, choices=Gender.choices, default=Gender.MALE)
    phone = models.CharField("الجوال", max_length=20)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    birth_date = models.DateField("تاريخ الميلاد", null=True, blank=True)
    city = models.CharField("المدينة", max_length=60, blank=True)
    skills = models.CharField("المهارات والاهتمامات", max_length=255, blank=True)
    national_platform_id = models.CharField("رقم المتطوع في المنصة الوطنية للعمل التطوعي", max_length=30, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=VolunteerStatus.choices, default=VolunteerStatus.NEW)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "متطوع"
        verbose_name_plural = "المتطوعون"
        ordering = ["full_name"]
        constraints = [models.UniqueConstraint(fields=["company", "national_id"], name="volunteer_national_id_per_company",
                                               violation_error_message="يوجد متطوع مسجل بنفس رقم الهوية.")]

    def __str__(self):
        return self.full_name

    def total_hours(self, year=None):
        rows = self.participations.filter(status=ParticipationStatus.COMPLETED)
        if year:
            rows = rows.filter(opportunity__start_date__year=year)
        return flt(rows.aggregate(t=models.Sum("hours"))["t"])


class OpportunityStatus(models.TextChoices):
    OPEN = "open", "مفتوحة للتسجيل"
    CLOSED = "closed", "مغلقة"
    COMPLETED = "completed", "منفذة"


def new_token():
    return uuid.uuid4().hex


class VolunteerOpportunity(TenantModel):
    title = models.CharField("الفرصة التطوعية", max_length=200)
    project = models.ForeignKey("projects.Project", models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="المشروع / البرنامج")
    description = models.TextField("الوصف والمهام", blank=True)
    location = models.CharField("المكان", max_length=200, blank=True)
    start_date = models.DateField("من تاريخ")
    end_date = models.DateField("إلى تاريخ")
    required_count = models.PositiveSmallIntegerField("العدد المطلوب", default=10)
    expected_hours = models.DecimalField("الساعات المتوقعة للمتطوع", max_digits=6, decimal_places=1, default=4)
    gender = models.CharField("مخصصة لـ", max_length=6, choices=Gender.choices, blank=True, help_text="فارغ = الجميع")
    hour_value = models.DecimalField("قيمة ساعة التطوع", max_digits=8, decimal_places=2, default=0,
                                     help_text="لإثبات قيمة الخدمات التطوعية محاسبياً (صفر = لا تُثبت)")
    status = models.CharField("الحالة", max_length=10, choices=OpportunityStatus.choices,
                              default=OpportunityStatus.OPEN, editable=False)
    public_token = models.CharField(max_length=32, default=new_token, unique=True, editable=False)
    value_entry = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True, related_name="+",
                                    editable=False, verbose_name="قيد قيمة التطوع")

    class Meta:
        verbose_name = "فرصة تطوعية"
        verbose_name_plural = "الفرص التطوعية"
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ النهاية يجب أن يكون بعد البداية."})

    @property
    def accepted_count(self):
        return self.participations.filter(status__in=[ParticipationStatus.ACCEPTED,
                                                      ParticipationStatus.COMPLETED]).count()

    @property
    def is_full(self):
        return self.accepted_count >= self.required_count

    @property
    def completed_hours(self):
        return flt(self.participations.filter(status=ParticipationStatus.COMPLETED)
                   .aggregate(t=models.Sum("hours"))["t"])

    @property
    def available_actions(self):
        actions = []
        if self.status == OpportunityStatus.OPEN:
            actions.append(("close_registration", "إغلاق التسجيل"))
        if self.status == OpportunityStatus.CLOSED:
            actions.append(("reopen", "إعادة فتح التسجيل"))
        if self.status != OpportunityStatus.COMPLETED:
            actions.append(("complete", "اعتماد تنفيذ الفرصة"))
        if self.status == OpportunityStatus.COMPLETED and self.hour_value and self.value_entry_id is None:
            actions.append(("record_value", "إثبات قيمة التطوع محاسبياً"))
        return actions

    def close_registration(self, user):
        self.status = OpportunityStatus.CLOSED
        self.save(update_fields=["status"])

    def reopen(self, user):
        self.status = OpportunityStatus.OPEN
        self.save(update_fields=["status"])

    def complete(self, user):
        """المقبولون يُعتمد حضورهم بساعاتهم (أو الساعات المتوقعة إن لم تُسجل) وتصدر شهاداتهم."""
        for participation in self.participations.filter(status=ParticipationStatus.ACCEPTED):
            participation.complete(user)
        self.status = OpportunityStatus.COMPLETED
        self.save(update_fields=["status"])

    def record_value(self, user):
        """مسودة قيد بقيمة ساعات التطوع: مصروف خدمات تطوعية مقابل تبرعات خدمات تطوعية (غير مقيدة)."""
        from accounts.models import JournalEntry
        from awqaf.models import account_by_number

        value = flt(self.completed_hours * self.hour_value)
        if value <= 0:
            raise ValidationError("لا توجد ساعات تطوع منفذة لإثبات قيمتها.")
        entry = JournalEntry.objects.create(posting_date=self.end_date, created_by=user,
                                            remarks=f"قيمة ساعات التطوع: {self.title} ({self.completed_hours} ساعة)")
        entry.accounts.create(account=account_by_number(VOLUNTEER_EXPENSE_ACCOUNT), debit=value)
        entry.accounts.create(account=account_by_number(VOLUNTEER_INCOME_ACCOUNT), credit=value)
        entry.calculate_totals()
        entry.save()
        self.value_entry = entry
        self.save(update_fields=["value_entry"])
        return entry


class ParticipationStatus(models.TextChoices):
    REGISTERED = "registered", "طلب تسجيل"
    ACCEPTED = "accepted", "مقبول"
    REJECTED = "rejected", "معتذر عنه"
    COMPLETED = "completed", "أتم المشاركة"
    ABSENT = "absent", "لم يحضر"


def new_certificate_code():
    return secrets.token_hex(5).upper()


class VolunteerParticipation(TenantModel):
    opportunity = models.ForeignKey(VolunteerOpportunity, models.CASCADE, related_name="participations",
                                    verbose_name="الفرصة")
    volunteer = models.ForeignKey(Volunteer, models.PROTECT, related_name="participations", verbose_name="المتطوع")
    status = models.CharField("الحالة", max_length=12, choices=ParticipationStatus.choices,
                              default=ParticipationStatus.REGISTERED, editable=False)
    hours = models.DecimalField("الساعات المعتمدة", max_digits=6, decimal_places=1, default=0)
    rating = models.PositiveSmallIntegerField("التقييم (1-5)", null=True, blank=True,
                                              choices=[(i, str(i)) for i in range(1, 6)])
    notes = models.CharField("ملاحظات", max_length=255, blank=True)
    certificate_code = models.CharField("رمز الشهادة", max_length=12, blank=True, editable=False)
    registered_at = models.DateTimeField("تاريخ التسجيل", auto_now_add=True)

    class Meta:
        verbose_name = "مشاركة تطوعية"
        verbose_name_plural = "المشاركات التطوعية"
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["opportunity", "volunteer"], name="one_participation_per_volunteer",
                                               violation_error_message="المتطوع مسجل في هذه الفرصة مسبقاً.")]

    def __str__(self):
        return f"{self.volunteer} — {self.opportunity}"

    def clean(self):
        if self.hours is not None and self.hours < 0:
            raise ValidationError({"hours": "الساعات لا تكون سالبة."})

    @property
    def available_actions(self):
        if self.status == ParticipationStatus.REGISTERED:
            return [("accept", "قبول"), ("decline", "اعتذار")]
        if self.status == ParticipationStatus.ACCEPTED:
            return [("complete", "اعتماد المشاركة وإصدار الشهادة"), ("mark_absent", "لم يحضر")]
        return []

    def accept(self, user):
        if self.opportunity.is_full:
            raise ValidationError("اكتمل العدد المطلوب للفرصة.")
        self.status = ParticipationStatus.ACCEPTED
        self.save(update_fields=["status"])
        if self.volunteer.status == VolunteerStatus.NEW:
            self.volunteer.status = VolunteerStatus.APPROVED
            self.volunteer.save(update_fields=["status"])

    def decline(self, user):
        self.status = ParticipationStatus.REJECTED
        self.save(update_fields=["status"])

    def mark_absent(self, user):
        self.status = ParticipationStatus.ABSENT
        self.save(update_fields=["status"])

    def complete(self, user):
        if not self.hours:
            self.hours = self.opportunity.expected_hours or Decimal(0)
        self.status = ParticipationStatus.COMPLETED
        if not self.certificate_code:
            self.certificate_code = new_certificate_code()
        self.save(update_fields=["hours", "status", "certificate_code"])
