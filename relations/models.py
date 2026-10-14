"""العلاقات العامة والإعلام: جهات التواصل، الشراكات، الفعاليات والحملات، الزيارات، التغطيات الإعلامية، واستطلاعات الرأي
(باستبانة عامة برابط تُعبأ دون تسجيل دخول)."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TenantModel


class ContactCategory(models.TextChoices):
    GOVERNMENT = "government", "جهة حكومية"
    PRIVATE = "private", "قطاع خاص"
    MEDIA = "media", "إعلامي"
    SUPPORTER = "supporter", "داعم"
    PARTNER = "partner", "شريك"
    NONPROFIT = "nonprofit", "جمعية"
    OTHER = "other", "أخرى"


class PRContact(TenantModel):
    name = models.CharField("الاسم", max_length=150)
    organization = models.CharField("الجهة", max_length=200, blank=True)
    job_title = models.CharField("المسمى الوظيفي", max_length=100, blank=True)
    category = models.CharField("التصنيف", max_length=12, choices=ContactCategory.choices,
                                default=ContactCategory.GOVERNMENT)
    phone = models.CharField("الجوال", max_length=20, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=60, blank=True)
    notes = models.TextField("ملاحظات", blank=True)
    is_active = models.BooleanField("نشط", default=True)

    class Meta:
        verbose_name = "جهة تواصل"
        verbose_name_plural = "جهات التواصل"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} — {self.organization}" if self.organization else self.name


class PartnershipType(models.TextChoices):
    STRATEGIC = "strategic", "استراتيجية"
    EXECUTIVE = "executive", "تنفيذية"
    MEDIA = "media", "إعلامية"
    FUNDING = "funding", "تمويلية"
    COMMUNITY = "community", "مجتمعية"


class PartnershipStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    ACTIVE = "active", "سارية"
    ENDED = "ended", "منتهية"
    CANCELLED = "cancelled", "ملغاة"


class Partnership(TenantModel):
    partner = models.CharField("الجهة الشريكة", max_length=200)
    contact = models.ForeignKey(PRContact, models.SET_NULL, null=True, blank=True, related_name="partnerships",
                                verbose_name="جهة التواصل")
    partnership_type = models.CharField("نوع الشراكة", max_length=10, choices=PartnershipType.choices,
                                        default=PartnershipType.EXECUTIVE)
    agreement_number = models.CharField("رقم الاتفاقية", max_length=50, blank=True)
    start_date = models.DateField("تاريخ البداية")
    end_date = models.DateField("تاريخ النهاية", null=True, blank=True, help_text="فارغ = مفتوحة المدة")
    value = models.DecimalField("قيمة الشراكة", max_digits=14, decimal_places=2, null=True, blank=True)
    objectives = models.TextField("الأهداف ومجالات التعاون", blank=True)
    status = models.CharField("الحالة", max_length=10, choices=PartnershipStatus.choices,
                              default=PartnershipStatus.DRAFT, editable=False)

    class Meta:
        verbose_name = "شراكة"
        verbose_name_plural = "الشراكات"
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return f"{self.partner} ({self.get_partnership_type_display()})"

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ النهاية يجب أن يكون بعد البداية."})
        if self.value is not None and self.value < 0:
            raise ValidationError({"value": "القيمة لا تكون سالبة."})

    @property
    def days_to_expiry(self):
        """الأيام المتبقية على انتهاء الاتفاقية (سالبة إن انتهت، None إن كانت مفتوحة المدة)."""
        if not self.end_date:
            return None
        return (self.end_date - timezone.localdate()).days

    @property
    def available_actions(self):
        if self.status == PartnershipStatus.DRAFT:
            return [("activate", "تفعيل الشراكة"), ("cancel_partnership", "إلغاء")]
        if self.status == PartnershipStatus.ACTIVE:
            return [("terminate", "إنهاء الشراكة")]
        return []

    def activate(self, user):
        if self.end_date and self.end_date < timezone.localdate():
            raise ValidationError("لا يمكن تفعيل شراكة انتهى تاريخها.")
        self.status = PartnershipStatus.ACTIVE
        self.save(update_fields=["status"])

    def terminate(self, user):
        self.status = PartnershipStatus.ENDED
        self.save(update_fields=["status"])

    def cancel_partnership(self, user):
        self.status = PartnershipStatus.CANCELLED
        self.save(update_fields=["status"])


class EventType(models.TextChoices):
    EVENT = "event", "فعالية"
    CAMPAIGN = "campaign", "حملة إعلامية"
    EXHIBITION = "exhibition", "معرض"
    FORUM = "forum", "ملتقى"
    INITIATIVE = "initiative", "مبادرة مجتمعية"
    FIELD_VISIT = "field_visit", "زيارة ميدانية"


class EventStatus(models.TextChoices):
    PLANNED = "planned", "مخطط"
    DONE = "done", "منفذ"
    CANCELLED = "cancelled", "ملغى"


class PREvent(TenantModel):
    title = models.CharField("العنوان", max_length=200)
    event_type = models.CharField("النوع", max_length=12, choices=EventType.choices, default=EventType.EVENT)
    start_date = models.DateField("من تاريخ")
    end_date = models.DateField("إلى تاريخ", null=True, blank=True, help_text="فارغ = يوم واحد")
    location = models.CharField("المكان", max_length=200, blank=True)
    partner = models.ForeignKey(Partnership, models.SET_NULL, null=True, blank=True, related_name="events",
                                verbose_name="الشراكة")
    budget = models.DecimalField("الميزانية التقديرية", max_digits=14, decimal_places=2, default=0)
    actual_cost = models.DecimalField("التكلفة الفعلية", max_digits=14, decimal_places=2, default=0)
    expected_attendees = models.PositiveIntegerField("الحضور المتوقع", default=0)
    actual_attendees = models.PositiveIntegerField("الحضور الفعلي", null=True, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=EventStatus.choices, default=EventStatus.PLANNED,
                              editable=False)
    outcomes = models.TextField("المخرجات والنتائج", blank=True)

    class Meta:
        verbose_name = "فعالية / حملة"
        verbose_name_plural = "الفعاليات والحملات"
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ النهاية يجب أن يكون بعد البداية."})
        for name in ("budget", "actual_cost"):
            if getattr(self, name) is not None and getattr(self, name) < 0:
                raise ValidationError({name: "القيمة لا تكون سالبة."})

    @property
    def available_actions(self):
        if self.status == EventStatus.PLANNED:
            return [("mark_done", "اعتماد التنفيذ"), ("cancel_event", "إلغاء")]
        return []

    def mark_done(self, user):
        if not self.actual_attendees:
            raise ValidationError("سجّل عدد الحضور الفعلي قبل اعتماد التنفيذ.")
        self.status = EventStatus.DONE
        self.save(update_fields=["status"])

    def cancel_event(self, user):
        self.status = EventStatus.CANCELLED
        self.save(update_fields=["status"])


class VisitDirection(models.TextChoices):
    INCOMING = "incoming", "واردة"
    OUTGOING = "outgoing", "صادرة"


class VisitStatus(models.TextChoices):
    SCHEDULED = "scheduled", "مجدولة"
    DONE = "done", "تمت"
    CANCELLED = "cancelled", "ملغاة"


class Visit(TenantModel):
    direction = models.CharField("نوع الزيارة", max_length=10, choices=VisitDirection.choices,
                                 default=VisitDirection.INCOMING)
    entity = models.CharField("الجهة", max_length=200)
    contact = models.ForeignKey(PRContact, models.SET_NULL, null=True, blank=True, related_name="visits",
                                verbose_name="جهة التواصل")
    visit_date = models.DateField("تاريخ الزيارة")
    purpose = models.CharField("الغرض", max_length=255)
    attendees = models.TextField("الحضور", blank=True)
    outcomes = models.TextField("المخرجات والتوصيات", blank=True)
    follow_up_date = models.DateField("تاريخ المتابعة", null=True, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=VisitStatus.choices, default=VisitStatus.SCHEDULED)

    class Meta:
        verbose_name = "زيارة"
        verbose_name_plural = "الزيارات"
        ordering = ["-visit_date", "-id"]

    def __str__(self):
        return f"زيارة {self.get_direction_display()}: {self.entity} ({self.visit_date:%Y-%m-%d})"

    def clean(self):
        if self.visit_date and self.follow_up_date and self.follow_up_date < self.visit_date:
            raise ValidationError({"follow_up_date": "تاريخ المتابعة يجب أن يكون بعد الزيارة."})


class MediaChannel(models.TextChoices):
    NEWSPAPER = "newspaper", "صحيفة"
    TV = "tv", "تلفزيون"
    RADIO = "radio", "إذاعة"
    WEBSITE = "website", "موقع إلكتروني"
    SOCIAL = "social", "تواصل اجتماعي"


class MediaCoverage(TenantModel):
    title = models.CharField("العنوان", max_length=255)
    channel = models.CharField("الوسيلة", max_length=10, choices=MediaChannel.choices, default=MediaChannel.NEWSPAPER)
    published_on = models.DateField("تاريخ النشر")
    url = models.URLField("الرابط", max_length=500, blank=True)
    reach = models.PositiveIntegerField("الوصول / المشاهدات", null=True, blank=True)
    event = models.ForeignKey(PREvent, models.SET_NULL, null=True, blank=True, related_name="coverages",
                              verbose_name="الفعالية")

    class Meta:
        verbose_name = "تغطية إعلامية"
        verbose_name_plural = "التغطيات الإعلامية"
        ordering = ["-published_on", "-id"]

    def __str__(self):
        return self.title


def new_token():
    return uuid.uuid4().hex


class Survey(TenantModel):
    title = models.CharField("عنوان الاستبيان", max_length=200)
    description = models.TextField("المقدمة", blank=True)
    audience = models.CharField("الفئة المستهدفة", max_length=150, blank=True)
    is_open = models.BooleanField("مفتوح لاستقبال الردود", default=True)
    end_date = models.DateField("آخر موعد", null=True, blank=True, help_text="فارغ = بلا موعد إغلاق")
    public_token = models.CharField(max_length=32, default=new_token, unique=True, editable=False)

    class Meta:
        verbose_name = "استبيان"
        verbose_name_plural = "استطلاعات الرأي"
        ordering = ["-id"]

    def __str__(self):
        return self.title

    @property
    def accepting(self):
        return self.is_open and (self.end_date is None or self.end_date >= timezone.localdate())


class QuestionType(models.TextChoices):
    RATING = "rating", "تقييم من 1 إلى 5"
    CHOICE = "choice", "اختيار من متعدد"
    TEXT = "text", "نص"


class SurveyQuestion(models.Model):
    parent = models.ForeignKey(Survey, models.CASCADE, related_name="questions")
    text = models.CharField("السؤال", max_length=255)
    question_type = models.CharField("نوع السؤال", max_length=8, choices=QuestionType.choices,
                                     default=QuestionType.RATING)
    choices = models.TextField("الخيارات", blank=True, help_text="خيار في كل سطر (لأسئلة الاختيار من متعدد)")
    required = models.BooleanField("إجباري", default=True)

    class Meta:
        verbose_name = "سؤال"
        verbose_name_plural = "الأسئلة"
        ordering = ["id"]

    def __str__(self):
        return self.text

    @property
    def choice_list(self):
        return [line.strip() for line in self.choices.splitlines() if line.strip()]

    def clean(self):
        if self.question_type == QuestionType.CHOICE and len(self.choice_list) < 2:
            raise ValidationError({"choices": "أدخل خيارين على الأقل، كل خيار في سطر."})


class SurveyResponse(TenantModel):
    survey = models.ForeignKey(Survey, models.CASCADE, related_name="responses", verbose_name="الاستبيان")
    submitted_at = models.DateTimeField("وقت الإرسال", auto_now_add=True)
    answers = models.JSONField("الإجابات", default=dict)  # {معرّف السؤال (نص): الإجابة}

    class Meta:
        verbose_name = "رد على استبيان"
        verbose_name_plural = "ردود الاستبيانات"
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.survey} — {self.submitted_at:%Y-%m-%d %H:%M}"
