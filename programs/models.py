"""إدارة البرامج والتطوير: البرامج التدريبية وجلساتها، تسجيل المتدربين (برابط عام)، الحضور والاختبارات القبلية والبعدية،
الاجتياز والشهادات، تحصيل رسوم البرامج بسند تبرع، ومؤشرات أثر التدريب."""
import secrets
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from beneficiaries.models import Gender, validate_national_id
from core.models import TenantModel
from core.utils import flt

PROGRAM_FEES_ACCOUNT = "31205005"  # إيرادات غير مقيدة - رسوم


class ProgramType(models.TextChoices):
    COURSE = "course", "دورة تدريبية"
    WORKSHOP = "workshop", "ورشة عمل"
    QUALIFYING = "qualifying", "برنامج تأهيلي"
    LECTURE = "lecture", "محاضرة"
    AWARENESS = "awareness", "برنامج توعوي"


class DeliveryMode(models.TextChoices):
    ONSITE = "onsite", "حضوري"
    REMOTE = "remote", "عن بعد"
    BLENDED = "blended", "مدمج"


class ProgramStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    OPEN = "open", "التسجيل مفتوح"
    RUNNING = "running", "جارٍ"
    COMPLETED = "completed", "مكتمل"
    CANCELLED = "cancelled", "ملغى"


def new_token():
    return uuid.uuid4().hex


def new_certificate_code():
    return secrets.token_hex(5).upper()


def _avg(values):
    values = [Decimal(v) for v in values if v is not None]
    return flt(sum(values) / len(values)) if values else None


class TrainingProgram(TenantModel):
    title = models.CharField("البرنامج", max_length=200)
    program_type = models.CharField("نوع البرنامج", max_length=12, choices=ProgramType.choices,
                                    default=ProgramType.COURSE)
    track = models.CharField("المسار", max_length=100, blank=True, help_text="مثل: مسار القيادة، مسار التمكين المهني")
    trainer = models.CharField("المدرب", max_length=150, blank=True)
    location = models.CharField("المكان / رابط البث", max_length=200, blank=True)
    mode = models.CharField("طريقة التنفيذ", max_length=8, choices=DeliveryMode.choices, default=DeliveryMode.ONSITE)
    start_date = models.DateField("تاريخ البداية")
    end_date = models.DateField("تاريخ النهاية")
    hours = models.DecimalField("الساعات التدريبية", max_digits=6, decimal_places=1, default=0)
    capacity = models.PositiveSmallIntegerField("المقاعد المتاحة", default=25)
    fee = models.DecimalField("رسوم التسجيل", max_digits=10, decimal_places=2, default=0, help_text="صفر = مجاني")
    target_audience = models.CharField("الفئة المستهدفة", max_length=200, blank=True)
    description = models.TextField("الوصف والمحاور", blank=True)
    pass_score = models.PositiveSmallIntegerField("درجة الاجتياز (الاختبار البعدي)", default=60,
                                                  validators=[MaxValueValidator(100)])
    min_attendance = models.PositiveSmallIntegerField("الحد الأدنى للحضور %", default=75,
                                                      validators=[MaxValueValidator(100)])
    status = models.CharField("الحالة", max_length=10, choices=ProgramStatus.choices, default=ProgramStatus.DRAFT,
                              editable=False)
    public_token = models.CharField(max_length=32, default=new_token, unique=True, editable=False)

    class Meta:
        verbose_name = "برنامج تدريبي"
        verbose_name_plural = "البرامج التدريبية"
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ النهاية يجب أن يكون بعد البداية."})
        if self.fee is not None and self.fee < 0:
            raise ValidationError({"fee": "الرسوم لا تكون سالبة."})

    @property
    def admitted(self):
        """المتدربون المقبولون (قبل التقييم وبعده)."""
        return self.enrollments.filter(status__in=ADMITTED_STATUSES)

    @property
    def accepted_count(self):
        return self.admitted.count()

    @property
    def is_full(self):
        return self.accepted_count >= self.capacity

    @property
    def available_actions(self):
        actions = []
        if self.status == ProgramStatus.DRAFT:
            actions.append(("open_registration", "فتح التسجيل"))
        if self.status == ProgramStatus.OPEN:
            actions.append(("start", "إغلاق التسجيل وبدء البرنامج"))
        if self.status in (ProgramStatus.OPEN, ProgramStatus.RUNNING):
            actions.append(("complete", "اعتماد اكتمال البرنامج وتقييم المتدربين"))
        if self.status in (ProgramStatus.DRAFT, ProgramStatus.OPEN, ProgramStatus.RUNNING):
            actions.append(("cancel", "إلغاء البرنامج"))
        return actions

    def _set_status(self, status):
        self.status = status
        self.save(update_fields=["status"])

    def open_registration(self, user):
        self._set_status(ProgramStatus.OPEN)

    def start(self, user):
        self._set_status(ProgramStatus.RUNNING)

    close_registration = start

    def cancel(self, user):
        if self.status == ProgramStatus.COMPLETED:
            raise ValidationError("لا يُلغى برنامج مكتمل.")
        self._set_status(ProgramStatus.CANCELLED)

    def complete(self, user):
        """يُقيّم كل متدرب مقبول: يجتاز إن بلغ حضوره الحد الأدنى وبلغت درجته البعدية درجة الاجتياز (إن سُجلت)."""
        if self.status in (ProgramStatus.COMPLETED, ProgramStatus.CANCELLED):
            raise ValidationError("البرنامج مكتمل أو ملغى.")
        for enrollment in self.enrollments.filter(status=EnrollmentStatus.ACCEPTED):
            enrollment.evaluate()
        self._set_status(ProgramStatus.COMPLETED)

    def impact(self):
        """مؤشرات الأثر: الملتحقون، المجتازون، نسبة الإتمام، متوسط القبلي والبعدي، نسبة التحسن، ومتوسط الرضا."""
        rows = list(self.admitted.values("status", "pre_score", "post_score", "satisfaction"))
        enrolled = len(rows)
        completed = sum(1 for r in rows if r["status"] == EnrollmentStatus.PASSED)
        avg_pre = _avg(r["pre_score"] for r in rows)
        avg_post = _avg(r["post_score"] for r in rows)
        return {
            "enrolled": enrolled, "completed": completed,
            "completion_rate": flt(Decimal(completed) * 100 / enrolled, 1) if enrolled else None,
            "avg_pre": avg_pre, "avg_post": avg_post,
            "improvement": flt((avg_post - avg_pre) * 100 / avg_pre, 1) if avg_pre and avg_post is not None else None,
            "avg_satisfaction": _avg(r["satisfaction"] for r in rows),
            "trained_hours": flt(self.hours * completed, 1),
        }


class ProgramSession(models.Model):
    parent = models.ForeignKey(TrainingProgram, models.CASCADE, related_name="sessions")
    date = models.DateField("التاريخ")
    topic = models.CharField("موضوع الجلسة", max_length=200, blank=True)

    class Meta:
        verbose_name = "جلسة"
        verbose_name_plural = "الجلسات"
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.date:%Y-%m-%d} {self.topic}".strip()


class EnrollmentStatus(models.TextChoices):
    REGISTERED = "registered", "مسجل"
    ACCEPTED = "accepted", "مقبول"
    PASSED = "passed", "مجتاز"
    FAILED = "failed", "غير مجتاز"
    WITHDRAWN = "withdrawn", "منسحب"


ADMITTED_STATUSES = [EnrollmentStatus.ACCEPTED, EnrollmentStatus.PASSED, EnrollmentStatus.FAILED]
SCORE_VALIDATORS = [MinValueValidator(0), MaxValueValidator(100)]


class ProgramEnrollment(TenantModel):
    program = models.ForeignKey(TrainingProgram, models.CASCADE, related_name="enrollments", verbose_name="البرنامج")
    full_name = models.CharField("الاسم الكامل", max_length=150)
    national_id = models.CharField("رقم الهوية / الإقامة", max_length=10, validators=[validate_national_id])
    phone = models.CharField("الجوال", max_length=20)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    gender = models.CharField("الجنس", max_length=6, choices=Gender.choices, default=Gender.MALE)
    beneficiary = models.ForeignKey("beneficiaries.Beneficiary", models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="المستفيد", help_text="إن كان المتدرب من المستفيدين")
    status = models.CharField("الحالة", max_length=10, choices=EnrollmentStatus.choices,
                              default=EnrollmentStatus.REGISTERED, editable=False)
    pre_score = models.DecimalField("درجة الاختبار القبلي", max_digits=5, decimal_places=1, null=True, blank=True,
                                    validators=SCORE_VALIDATORS)
    post_score = models.DecimalField("درجة الاختبار البعدي", max_digits=5, decimal_places=1, null=True, blank=True,
                                     validators=SCORE_VALIDATORS)
    satisfaction = models.PositiveSmallIntegerField("الرضا عن البرنامج (1-5)", null=True, blank=True,
                                                    choices=[(i, str(i)) for i in range(1, 6)])
    notes = models.CharField("ملاحظات", max_length=255, blank=True)
    certificate_code = models.CharField("رمز الشهادة", max_length=12, blank=True, editable=False)
    fee_receipt = models.ForeignKey("funds.DonationReceipt", models.SET_NULL, null=True, blank=True, related_name="+",
                                    editable=False, verbose_name="سند الرسوم")
    registered_at = models.DateTimeField("تاريخ التسجيل", auto_now_add=True)

    class Meta:
        verbose_name = "متدرب في برنامج"
        verbose_name_plural = "المتدربون في البرامج"
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["company", "program", "national_id"],
                                               name="programenrollment_program_national_id_per_company",
                                               violation_error_message="المتدرب مسجل في هذا البرنامج مسبقاً.")]

    def __str__(self):
        return f"{self.full_name} — {self.program}"

    def clean(self):
        if self.beneficiary_id and not self.full_name:
            self.full_name = self.beneficiary.full_name

    @property
    def attendance_percent(self):
        """نسبة الجلسات التي حضرها من جلسات البرنامج (برنامج بلا جلسات = 100%)."""
        sessions = self.program.sessions.count()
        if not sessions:
            return Decimal(100)
        present = self.attendance.filter(present=True, session__parent=self.program_id).count()
        return flt(Decimal(present) * 100 / sessions, 1)

    @property
    def fee_paid(self):
        receipt = self.fee_receipt
        return receipt is not None and receipt.is_submitted

    @property
    def fee_status(self):
        if not self.program.fee:
            return "مجاني"
        if self.fee_receipt_id is None or self.fee_receipt.is_cancelled:
            return "غير مسددة"
        return "مسددة" if self.fee_receipt.is_submitted else "سند مسودة"

    @property
    def can_collect_fee(self):
        return (self.program.fee > 0 and self.status != EnrollmentStatus.WITHDRAWN
                and (self.fee_receipt_id is None or self.fee_receipt.is_cancelled))

    @property
    def available_actions(self):
        actions = []
        if self.status == EnrollmentStatus.REGISTERED:
            actions.append(("accept", "قبول"))
        if self.status in (EnrollmentStatus.REGISTERED, EnrollmentStatus.ACCEPTED):
            actions.append(("withdraw", "انسحاب"))
        if self.can_collect_fee:
            actions.append(("collect_fee", "تحصيل الرسوم"))
        return actions

    def accept(self, user):
        if self.program.status in (ProgramStatus.COMPLETED, ProgramStatus.CANCELLED):
            raise ValidationError("البرنامج مكتمل أو ملغى.")
        if self.program.is_full:
            raise ValidationError("اكتملت المقاعد المتاحة في البرنامج.")
        self.status = EnrollmentStatus.ACCEPTED
        self.save(update_fields=["status"])

    def withdraw(self, user):
        self.status = EnrollmentStatus.WITHDRAWN
        self.save(update_fields=["status"])

    def evaluate(self):
        program = self.program
        passed = (self.attendance_percent >= program.min_attendance
                  and (self.post_score is None or self.post_score >= program.pass_score))
        self.status = EnrollmentStatus.PASSED if passed else EnrollmentStatus.FAILED
        if passed and not self.certificate_code:
            self.certificate_code = new_certificate_code()
        self.save(update_fields=["status", "certificate_code"])

    def collect_fee(self, user):
        """مسودة سند تبرع عام برسوم البرنامج على حساب «رسوم» (31205005) وتُربط بالمتدرب."""
        from accounts.models import Account
        from funds.models import DonationReceipt, DonationType

        if not self.can_collect_fee:
            raise ValidationError("لا رسوم مستحقة على هذا المتدرب أو سبق إصدار سندها.")
        if self.company.default_cash_account_id is None:
            raise ValidationError("حدد الصندوق الافتراضي في إعدادات الشركة أولاً.")
        receipt = DonationReceipt.objects.create(
            donor_name=self.full_name, donor_phone=self.phone, donation_type=DonationType.GENERAL,
            income_account=Account.objects.filter(number=PROGRAM_FEES_ACCOUNT, is_group=False).first(),
            amount=self.program.fee, account=self.company.default_cash_account, created_by=user,
            remarks=f"رسوم البرنامج «{self.program.title}» — {self.full_name}")
        self.fee_receipt = receipt
        self.save(update_fields=["fee_receipt"])
        return receipt


class SessionAttendance(TenantModel):
    enrollment = models.ForeignKey(ProgramEnrollment, models.CASCADE, related_name="attendance",
                                   verbose_name="المتدرب")
    session = models.ForeignKey(ProgramSession, models.CASCADE, related_name="attendance", verbose_name="الجلسة")
    present = models.BooleanField("حاضر", default=False)

    class Meta:
        verbose_name = "حضور جلسة"
        verbose_name_plural = "حضور الجلسات"
        constraints = [models.UniqueConstraint(fields=["enrollment", "session"], name="one_attendance_per_session")]

    def __str__(self):
        return f"{self.enrollment.full_name} — {self.session}"
