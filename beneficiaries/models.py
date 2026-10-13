"""إدارة التقييم والمتابعة (ملف المستفيد ودراسة الحالة) وإدارة الجهات المستفيدة للجهات غير الربحية."""
import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import NamingSeries, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


def add_months(date, months):
    month = date.month - 1 + months
    year, month = date.year + month // 12, month % 12 + 1
    for day in (date.day, 30, 29, 28):
        try:
            return date.replace(year=year, month=month, day=day)
        except ValueError:
            continue


def validate_national_id(value):
    """الهوية الوطنية تبدأ بـ1 والإقامة بـ2، وكلاهما 10 أرقام."""
    if value and not re.fullmatch(r"[12]\d{9}", value):
        raise ValidationError("رقم الهوية أو الإقامة يجب أن يكون 10 أرقام ويبدأ بـ1 (مواطن) أو 2 (مقيم).")


def validate_iban(value):
    if value and not re.fullmatch(r"SA\d{22}", value.replace(" ", "").upper()):
        raise ValidationError("رقم الآيبان السعودي يبدأ بـSA ويليه 22 رقماً.")


class Gender(models.TextChoices):
    MALE = "male", "ذكر"
    FEMALE = "female", "أنثى"


# ───────────────────────────── إدارة التقييم والمتابعة ─────────────────────────────

class BeneficiaryCategory(TenantModel):
    name = models.CharField("الفئة", max_length=100)
    description = models.CharField("الوصف", max_length=255, blank=True)

    class Meta:
        verbose_name = "فئة مستفيدين"
        verbose_name_plural = "فئات المستفيدين"
        ordering = ["id"]
        constraints = [tenant_unique("name", model_label="beneficiarycategory")]

    def __str__(self):
        return self.name


class AssessmentCriterion(TenantModel):
    name = models.CharField("المعيار", max_length=150)
    max_score = models.PositiveSmallIntegerField("الدرجة القصوى", default=10)
    description = models.CharField("إرشاد للباحث", max_length=255, blank=True)
    sort_order = models.PositiveSmallIntegerField("الترتيب", default=0)
    is_active = models.BooleanField("مفعّل", default=True)

    class Meta:
        verbose_name = "معيار دراسة الحالة"
        verbose_name_plural = "معايير دراسة الحالة"
        ordering = ["sort_order", "id"]
        constraints = [tenant_unique("name", model_label="assessmentcriterion")]

    def __str__(self):
        return self.name


class BeneficiaryStatus(models.TextChoices):
    NEW = "new", "جديد"
    UNDER_STUDY = "under_study", "قيد الدراسة"
    APPROVED = "approved", "معتمد"
    REJECTED = "rejected", "غير مستحق"
    SUSPENDED = "suspended", "موقوف"
    CLOSED = "closed", "مغلق"


class MaritalStatus(models.TextChoices):
    SINGLE = "single", "أعزب / عزباء"
    MARRIED = "married", "متزوج / متزوجة"
    DIVORCED = "divorced", "مطلق / مطلقة"
    WIDOWED = "widowed", "أرمل / أرملة"


class HousingType(models.TextChoices):
    OWNED = "owned", "ملك"
    RENTED = "rented", "إيجار"
    RELATIVES = "relatives", "مع الأقارب"
    CHARITY = "charity", "سكن خيري"
    OTHER = "other", "أخرى"


class Beneficiary(TenantModel):
    """ملف المستفيد (رب الأسرة) وأفراد أسرته."""

    file_number = models.CharField("رقم الملف", max_length=30, editable=False)
    full_name = models.CharField("الاسم الرباعي", max_length=150)
    national_id = models.CharField("رقم الهوية / الإقامة", max_length=10, validators=[validate_national_id])
    nationality = models.CharField("الجنسية", max_length=50, default="سعودي")
    gender = models.CharField("الجنس", max_length=6, choices=Gender.choices, default=Gender.FEMALE)
    birth_date = models.DateField("تاريخ الميلاد", null=True, blank=True)
    marital_status = models.CharField("الحالة الاجتماعية", max_length=10, choices=MaritalStatus.choices, blank=True)
    phone = models.CharField("الجوال", max_length=20)
    city = models.CharField("المدينة", max_length=60, blank=True)
    district = models.CharField("الحي", max_length=60, blank=True)
    address = models.CharField("العنوان التفصيلي", max_length=255, blank=True)
    category = models.ForeignKey(BeneficiaryCategory, models.PROTECT, null=True, blank=True, related_name="beneficiaries",
                                 verbose_name="الفئة")
    status = models.CharField("الحالة", max_length=12, choices=BeneficiaryStatus.choices, default=BeneficiaryStatus.NEW,
                              editable=False)
    housing_type = models.CharField("نوع السكن", max_length=10, choices=HousingType.choices, blank=True)
    annual_rent = models.DecimalField("الإيجار السنوي", max_digits=12, decimal_places=2, default=0)
    monthly_income = models.DecimalField("دخل رب الأسرة الشهري", max_digits=12, decimal_places=2, default=0,
                                         help_text="راتب، ضمان اجتماعي، حساب المواطن، تقاعد… (دخل الأفراد يُدخل في جدول الأسرة)")
    income_sources = models.CharField("مصادر الدخل", max_length=255, blank=True)
    monthly_obligations = models.DecimalField("الالتزامات الشهرية", max_digits=12, decimal_places=2, default=0,
                                              help_text="أقساط وديون موثقة تُخصم من الدخل عند حساب دخل الفرد")
    health_notes = models.TextField("الحالة الصحية للأسرة", blank=True)
    bank_name = models.CharField("البنك", max_length=60, blank=True)
    iban = models.CharField("رقم الآيبان", max_length=34, blank=True, validators=[validate_iban])
    researcher = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                   verbose_name="الباحث الاجتماعي")
    registered_on = models.DateField("تاريخ التسجيل", default=timezone.localdate)
    last_score = models.DecimalField("آخر درجة استحقاق %", max_digits=5, decimal_places=1, null=True, blank=True,
                                     editable=False)
    next_review_date = models.DateField("موعد إعادة الدراسة", null=True, blank=True, editable=False)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "مستفيد"
        verbose_name_plural = "المستفيدون"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "national_id"], name="beneficiary_national_id_per_company",
                                    violation_error_message="يوجد ملف مستفيد آخر بنفس رقم الهوية."),
            tenant_unique("file_number", model_label="beneficiary"),
        ]

    def __str__(self):
        return f"{self.file_number} — {self.full_name}" if self.file_number else self.full_name

    def clean(self):
        if self.iban:
            self.iban = self.iban.replace(" ", "").upper()
        if self.national_id:
            from core.models import Company

            company_id = self.company_id or getattr(Company.current(), "pk", None)
            member = (FamilyMember.objects.filter(national_id=self.national_id, parent__company_id=company_id)
                      .exclude(parent_id=self.pk).select_related("parent").first())
            if member is not None:
                raise ValidationError({"national_id": f"رقم الهوية مسجل كفرد في ملف {member.parent}."})

    def save(self, *args, **kwargs):
        if not self.file_number:
            from core.models import Company

            company = self.company if self.company_id else Company.current()
            self.file_number = NamingSeries.next_name(company, "BEN")
        super().save(*args, **kwargs)

    @property
    def family_size(self):
        return 1 + self.members.count()

    @property
    def family_income(self):
        return flt(self.monthly_income) + sum((flt(m.monthly_income) for m in self.members.all()), flt(0))

    @property
    def per_capita_income(self):
        """دخل الفرد الشهري = (دخل الأسرة − الالتزامات) ÷ عدد الأفراد."""
        return flt(max(self.family_income - flt(self.monthly_obligations), 0) / self.family_size)

    @property
    def review_due(self):
        return bool(self.next_review_date and self.next_review_date <= timezone.localdate())

    @property
    def available_actions(self):
        actions = []
        if self.status in (BeneficiaryStatus.APPROVED, BeneficiaryStatus.UNDER_STUDY, BeneficiaryStatus.NEW):
            actions.append(("suspend", "إيقاف الملف"))
        if self.status == BeneficiaryStatus.SUSPENDED:
            actions.append(("reactivate", "إعادة تفعيل الملف"))
        if self.status != BeneficiaryStatus.CLOSED:
            actions.append(("close_file", "إغلاق الملف"))
        return actions

    def suspend(self, user):
        self.status = BeneficiaryStatus.SUSPENDED
        self.save(update_fields=["status"])

    def reactivate(self, user):
        last = self.assessments.filter(docstatus=1).order_by("-posting_date", "-id").first()
        self.status = (BeneficiaryStatus.APPROVED if last and last.is_eligible else
                       BeneficiaryStatus.UNDER_STUDY if last is None else BeneficiaryStatus.REJECTED)
        self.save(update_fields=["status"])

    def close_file(self, user):
        self.status = BeneficiaryStatus.CLOSED
        self.save(update_fields=["status"])


class Relation(models.TextChoices):
    SPOUSE = "spouse", "زوج / زوجة"
    SON = "son", "ابن"
    DAUGHTER = "daughter", "ابنة"
    FATHER = "father", "أب"
    MOTHER = "mother", "أم"
    BROTHER = "brother", "أخ"
    SISTER = "sister", "أخت"
    OTHER = "other", "أخرى"


class FamilyMember(models.Model):
    parent = models.ForeignKey(Beneficiary, models.CASCADE, related_name="members")
    full_name = models.CharField("الاسم", max_length=150)
    national_id = models.CharField("رقم الهوية", max_length=10, blank=True, validators=[validate_national_id])
    relation = models.CharField("صلة القرابة", max_length=10, choices=Relation.choices)
    gender = models.CharField("الجنس", max_length=6, choices=Gender.choices)
    birth_date = models.DateField("تاريخ الميلاد", null=True, blank=True)
    education = models.CharField("التعليم / العمل", max_length=100, blank=True)
    health_status = models.CharField("الحالة الصحية", max_length=100, blank=True)
    monthly_income = models.DecimalField("الدخل الشهري", max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = "فرد أسرة"
        verbose_name_plural = "أفراد الأسرة"
        ordering = ["id"]

    def __str__(self):
        return self.full_name

    def clean(self):
        if not self.national_id:
            return
        head = Beneficiary.objects.filter(national_id=self.national_id).exclude(pk=self.parent_id).first()
        if head is not None:
            raise ValidationError({"national_id": f"رقم الهوية مسجل كرب أسرة في ملف {head}."})
        other = FamilyMember.objects.filter(national_id=self.national_id, parent__company_id=self._company_id())
        other = other.exclude(parent_id=self.parent_id).select_related("parent").first()
        if other is not None:
            raise ValidationError({"national_id": f"رقم الهوية مسجل كفرد في ملف {other.parent}."})

    def _company_id(self):
        if self.parent_id:
            return self.parent.company_id
        from core.models import Company

        current = Company.current()
        return current.pk if current else None


class VisitType(models.TextChoices):
    FIELD = "field", "زيارة ميدانية"
    OFFICE = "office", "مقابلة في مقر الجمعية"
    PHONE = "phone", "اتصال هاتفي"


class CaseAssessment(SubmittableDocument):
    """دراسة حالة بمعايير موزونة؛ اعتمادها يحدد استحقاق المستفيد وموعد إعادة دراسته."""

    naming_prefix = "CASE"
    posts_to_ledger = False
    posting_note = "اعتماد الدراسة يحدّث حالة المستفيد (معتمد أو غير مستحق) وموعد إعادة الدراسة."

    beneficiary = models.ForeignKey(Beneficiary, models.PROTECT, related_name="assessments", verbose_name="المستفيد")
    visit_type = models.CharField("طريقة الدراسة", max_length=10, choices=VisitType.choices, default=VisitType.FIELD)
    researcher = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                   verbose_name="الباحث الاجتماعي")
    category = models.ForeignKey(BeneficiaryCategory, models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="الفئة المقترحة", help_text="تُسند للمستفيد عند اعتماد الدراسة")
    findings = models.TextField("نتائج الدراسة وتوصية الباحث", blank=True)
    family_size = models.PositiveSmallIntegerField("عدد أفراد الأسرة", default=0, editable=False)
    per_capita_income = models.DecimalField("دخل الفرد الشهري", max_digits=12, decimal_places=2, default=0,
                                            editable=False)
    total_score = models.PositiveSmallIntegerField("مجموع الدرجات", default=0, editable=False)
    max_score = models.PositiveSmallIntegerField("الدرجة القصوى", default=0, editable=False)
    score_percent = models.DecimalField("درجة الاستحقاق %", max_digits=5, decimal_places=1, default=0, editable=False)
    is_eligible = models.BooleanField("مستحق", default=False, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "دراسة حالة"
        verbose_name_plural = "دراسات الحالة"

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft and not self.scores.exists():
            AssessmentScore.objects.bulk_create([
                AssessmentScore(parent=self, criterion=c.name, max_score=c.max_score)
                for c in AssessmentCriterion.objects.filter(company=self.company, is_active=True)])
        if self.is_draft:
            self.family_size = self.beneficiary.family_size
            self.per_capita_income = self.beneficiary.per_capita_income
            if self.beneficiary.status == BeneficiaryStatus.NEW:
                self.beneficiary.status = BeneficiaryStatus.UNDER_STUDY
                self.beneficiary.save(update_fields=["status"])
        scores = list(self.scores.all())
        self.total_score = sum(s.score or 0 for s in scores)
        self.max_score = sum(s.max_score for s in scores)
        self.score_percent = (Decimal(self.total_score * 100) / self.max_score).quantize(Decimal("0.1")) \
            if self.max_score else Decimal(0)
        self.is_eligible = bool(self.max_score) and self.score_percent >= self.company.eligibility_threshold

    def validate_for_submit(self):
        scores = list(self.scores.all())
        if not scores:
            raise ValidationError("لا توجد معايير في الدراسة. أضف معايير من «معايير دراسة الحالة».")
        missing = [s.criterion for s in scores if s.score is None]
        if missing:
            raise ValidationError("أدخل درجة لكل معيار: " + "، ".join(missing))
        if self.beneficiary.status == BeneficiaryStatus.CLOSED:
            raise ValidationError("ملف المستفيد مغلق.")

    def on_submit(self):
        beneficiary = self.beneficiary
        beneficiary.status = BeneficiaryStatus.APPROVED if self.is_eligible else BeneficiaryStatus.REJECTED
        beneficiary.last_score = self.score_percent
        beneficiary.next_review_date = add_months(self.posting_date, self.company.review_period_months)
        if self.is_eligible and self.category_id:
            beneficiary.category = self.category
        beneficiary.save(update_fields=["status", "last_score", "next_review_date", "category"])

    def on_cancel(self):
        beneficiary = self.beneficiary
        previous = beneficiary.assessments.filter(docstatus=1).exclude(pk=self.pk).order_by("-posting_date", "-id").first()
        if previous is None:
            beneficiary.status, beneficiary.last_score, beneficiary.next_review_date = \
                BeneficiaryStatus.UNDER_STUDY, None, None
        else:
            beneficiary.status = BeneficiaryStatus.APPROVED if previous.is_eligible else BeneficiaryStatus.REJECTED
            beneficiary.last_score = previous.score_percent
            beneficiary.next_review_date = add_months(previous.posting_date, self.company.review_period_months)
        beneficiary.save(update_fields=["status", "last_score", "next_review_date"])


class AssessmentScore(models.Model):
    parent = models.ForeignKey(CaseAssessment, models.CASCADE, related_name="scores")
    criterion = models.CharField("المعيار", max_length=150)
    max_score = models.PositiveSmallIntegerField("الدرجة القصوى", default=10)
    score = models.PositiveSmallIntegerField("الدرجة", null=True, blank=True)
    comment = models.CharField("ملاحظة الباحث", max_length=255, blank=True)

    class Meta:
        verbose_name = "درجة معيار"
        verbose_name_plural = "درجات المعايير"
        ordering = ["id"]

    def clean(self):
        if self.score is not None and self.score > self.max_score:
            raise ValidationError({"score": f"الدرجة لا تتجاوز {self.max_score}."})


# ───────────────────────────── إدارة الجهات المستفيدة ─────────────────────────────

class EntityType(models.TextChoices):
    GOVERNMENT = "government", "جهة حكومية"
    NONPROFIT = "nonprofit", "جمعية / جهة غير ربحية"
    DONOR = "donor", "جهة مانحة"
    PRIVATE = "private", "قطاع خاص"
    EDUCATION = "education", "جهة تعليمية"
    MOSQUE = "mosque", "مسجد / جامع"
    OTHER = "other", "أخرى"


class BeneficiaryEntity(TenantModel):
    name = models.CharField("اسم الجهة", max_length=150)
    entity_type = models.CharField("نوع الجهة", max_length=12, choices=EntityType.choices, default=EntityType.NONPROFIT)
    registration_number = models.CharField("رقم الترخيص / السجل", max_length=30, blank=True)
    representative = models.CharField("اسم الممثل", max_length=150, blank=True)
    representative_phone = models.CharField("جوال الممثل", max_length=20, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=60, blank=True)
    address = models.CharField("العنوان", max_length=255, blank=True)
    is_active = models.BooleanField("نشطة", default=True)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "جهة مستفيدة"
        verbose_name_plural = "الجهات المستفيدة"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="beneficiaryentity")]

    def __str__(self):
        return self.name


class RequestType(models.TextChoices):
    FINANCIAL = "financial", "دعم مالي"
    IN_KIND = "in_kind", "دعم عيني"
    PROGRAM = "program", "تنفيذ برنامج / فعالية"
    PARTNERSHIP = "partnership", "شراكة"
    OTHER = "other", "أخرى"


class RequestStatus(models.TextChoices):
    NEW = "new", "جديد"
    UNDER_REVIEW = "under_review", "قيد الدراسة"
    APPROVED = "approved", "معتمد"
    REJECTED = "rejected", "مرفوض"
    COMPLETED = "completed", "منفّذ"


class EntityRequest(TenantModel):
    number = models.CharField("رقم الطلب", max_length=30, editable=False)
    entity = models.ForeignKey(BeneficiaryEntity, models.PROTECT, related_name="requests", verbose_name="الجهة")
    request_date = models.DateField("تاريخ الطلب", default=timezone.localdate)
    request_type = models.CharField("نوع الطلب", max_length=12, choices=RequestType.choices,
                                    default=RequestType.FINANCIAL)
    subject = models.CharField("الموضوع", max_length=200)
    description = models.TextField("التفاصيل", blank=True)
    requested_amount = models.DecimalField("المبلغ المطلوب", max_digits=14, decimal_places=2, default=0)
    approved_amount = models.DecimalField("المبلغ المعتمد", max_digits=14, decimal_places=2, default=0,
                                          help_text="يُترك صفراً لاعتماد المبلغ المطلوب كاملاً")
    status = models.CharField("الحالة", max_length=12, choices=RequestStatus.choices, default=RequestStatus.NEW,
                              editable=False)
    decision_notes = models.TextField("ملاحظات القرار", blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                   editable=False, verbose_name="صاحب القرار")
    decided_at = models.DateTimeField("تاريخ القرار", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "طلب جهة"
        verbose_name_plural = "طلبات الجهات"
        ordering = ["-id"]
        constraints = [tenant_unique("number", model_label="entityrequest")]

    def __str__(self):
        return f"{self.number} — {self.subject}" if self.number else self.subject

    def clean(self):
        if self.approved_amount and self.requested_amount and self.approved_amount > self.requested_amount:
            raise ValidationError({"approved_amount": "المبلغ المعتمد لا يتجاوز المطلوب."})

    def save(self, *args, **kwargs):
        if not self.number:
            from core.models import Company

            company = self.company if self.company_id else Company.current()
            self.number = NamingSeries.next_name(company, "EREQ")
        super().save(*args, **kwargs)

    @property
    def available_actions(self):
        if self.status in (RequestStatus.NEW, RequestStatus.UNDER_REVIEW):
            actions = [("approve", "اعتماد الطلب"), ("decline", "رفض الطلب")]
            if self.status == RequestStatus.NEW:
                actions.insert(0, ("start_review", "بدء الدراسة"))
            return actions
        if self.status == RequestStatus.APPROVED:
            return [("complete", "تم التنفيذ")]
        return []

    def _decide(self, user, status):
        self.status, self.decided_by, self.decided_at = status, user, timezone.now()
        self.save(update_fields=["status", "decided_by", "decided_at", "approved_amount"])

    def start_review(self, user):
        self.status = RequestStatus.UNDER_REVIEW
        self.save(update_fields=["status"])

    def approve(self, user):
        if not self.approved_amount:
            self.approved_amount = self.requested_amount
        self._decide(user, RequestStatus.APPROVED)

    def decline(self, user):
        self.approved_amount = 0
        self._decide(user, RequestStatus.REJECTED)

    def complete(self, user):
        self.status = RequestStatus.COMPLETED
        self.save(update_fields=["status"])
