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
    categories = models.ManyToManyField("DependentCategory", blank=True, verbose_name="التصنيفات والحالات", related_name="members")
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
        self.validate_scores()
        from beneficiaries.committee import check_before_submit

        case = check_before_submit(self)
        if case is not None:  # قرار اللجنة هو الذي يحدد الاستحقاق
            self.is_eligible = case.status == "approved"

    def validate_scores(self):
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


# ───────────────────────────── صرف المساعدات ─────────────────────────────

class AidType(models.TextChoices):
    CASH = "cash", "نقدية"
    IN_KIND = "in_kind", "عينية (من المستودع)"
    VOUCHER = "voucher", "قسيمة شرائية لدى متجر شريك"


class AidProgram(TenantModel):
    """برنامج مساعدات: حساب مصروفه من فرع البرامج والأنشطة، وصندوقه، وشروط الاستحقاق."""

    name = models.CharField("اسم البرنامج", max_length=150)
    aid_type = models.CharField("نوع المساعدة", max_length=10, choices=AidType.choices, default=AidType.CASH)
    expense_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="حساب المصروف",
        limit_choices_to={"is_group": False, "root_type": "expense"},
        help_text="من مصاريف البرامج والأنشطة (421 المقيدة أو 422 غير المقيدة)")
    fund = models.ForeignKey("accounts.Fund", models.PROTECT, null=True, blank=True, related_name="+",
                             limit_choices_to={"is_active": True}, verbose_name="الصندوق",
                             help_text="يُصرف منه افتراضياً (مثل صندوق الزكاة)")
    default_amount = models.DecimalField("المبلغ الافتراضي", max_digits=14, decimal_places=2, default=0)
    max_per_year = models.PositiveSmallIntegerField("الحد الأقصى للصرف للمستفيد في السنة", default=0,
                                                    help_text="صفر = بلا حد")
    categories = models.ManyToManyField(BeneficiaryCategory, blank=True, related_name="+",
                                        verbose_name="الفئات المستحقة", help_text="فارغ = كل الفئات")
    voucher_valid_days = models.PositiveSmallIntegerField(
        "صلاحية القسيمة بالأيام", default=30, help_text="للقسائم الشرائية: ما لم يُستخدم بعدها يعود للبرنامج والصندوق")
    merchants = models.ManyToManyField("beneficiaries.VoucherMerchant", blank=True, related_name="programs",
                                       verbose_name="المتاجر المعتمدة للقسائم", help_text="فارغ = كل المتاجر الشريكة")
    is_active = models.BooleanField("مفعّل", default=True)
    description = models.TextField("الوصف والشروط", blank=True)

    class Meta:
        verbose_name = "برنامج مساعدات"
        verbose_name_plural = "برامج المساعدات"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="aidprogram")]

    def __str__(self):
        return self.name


class ReceiptMethod(models.TextChoices):
    OTP = "otp", "رمز تحقق على جوال المستفيد"
    MANUAL = "manual", "تأكيد يدوي من المدير"


OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


class AidDisbursement(SubmittableDocument):
    """صرف مساعدة نقدية أو عينية لمستفيد معتمد، تُقيد على مصروف البرنامج وصندوقه بعد تأكيد الاستلام."""

    naming_prefix = "AID"

    beneficiary = models.ForeignKey(Beneficiary, models.PROTECT, related_name="disbursements", verbose_name="المستفيد",
                                    limit_choices_to={"status": BeneficiaryStatus.APPROVED})
    program = models.ForeignKey(AidProgram, models.PROTECT, related_name="disbursements", verbose_name="البرنامج",
                                limit_choices_to={"is_active": True})
    aid_type = models.CharField("نوع المساعدة", max_length=10, choices=AidType.choices, editable=False,
                                default=AidType.CASH)
    amount = models.DecimalField("المبلغ", max_digits=14, decimal_places=2, default=0,
                                 help_text="للمساعدة النقدية؛ قيمة العينية تُحسب من تكلفة الأصناف عند الاعتماد")
    payment_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="الصرف من (صندوق / بنك)",
        limit_choices_to={"is_group": False, "account_type__in": ["cash", "bank"]})
    warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, null=True, blank=True, related_name="+",
                                  verbose_name="المستودع", help_text="للمساعدة العينية")
    fund = models.ForeignKey("accounts.Fund", models.PROTECT, null=True, blank=True, related_name="+",
                             limit_choices_to={"is_active": True}, verbose_name="الصندوق",
                             help_text="فارغ = صندوق البرنامج")
    otp_hash = models.CharField(max_length=64, blank=True, editable=False)
    otp_sent_at = models.DateTimeField("أُرسل رمز التحقق", null=True, blank=True, editable=False)
    otp_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    received_at = models.DateTimeField("تاريخ تأكيد الاستلام", null=True, blank=True, editable=False)
    receipt_method = models.CharField("طريقة تأكيد الاستلام", max_length=10, choices=ReceiptMethod.choices,
                                      blank=True, editable=False)
    receipt_note = models.CharField("ملاحظة التأكيد", max_length=255, blank=True, editable=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                     related_name="+", editable=False, verbose_name="أكد الاستلام")
    receipt_fingerprint = models.CharField(max_length=64, blank=True, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "صرف مساعدة"
        verbose_name_plural = "صرف المساعدات"

    def calculate_totals(self):
        if not self.program_id:
            return
        self.aid_type = self.program.aid_type
        if not self.fund_id and self.program.fund_id:
            self.fund = self.program.fund
        if self.aid_type in (AidType.CASH, AidType.VOUCHER):
            if not self.amount and self.program.default_amount:
                self.amount = self.program.default_amount
        if self.aid_type == AidType.CASH and not self.payment_account_id:
            self.payment_account = self.company.default_cash_account

    def clean(self):
        if self.amount is not None and self.amount < 0:
            raise ValidationError({"amount": "المبلغ لا يكون سالباً."})

    def check_eligibility(self):
        beneficiary, program = self.beneficiary, self.program
        if beneficiary.status != BeneficiaryStatus.APPROVED:
            raise ValidationError(f"المستفيد {beneficiary} حالته «{beneficiary.get_status_display()}»؛ "
                                  "الصرف للمستفيدين المعتمدين فقط.")
        if beneficiary.next_review_date and beneficiary.next_review_date < self.posting_date:
            raise ValidationError(f"انتهى موعد إعادة دراسة حالة {beneficiary} في {beneficiary.next_review_date:%Y-%m-%d}. "
                                  "أجرِ دراسة حالة جديدة قبل الصرف.")
        if not program.is_active:
            raise ValidationError(f"البرنامج «{program}» موقوف.")
        categories = list(program.categories.all())
        if categories and beneficiary.category not in categories:
            raise ValidationError(f"فئة المستفيد «{beneficiary.category or 'بدون فئة'}» ليست من فئات برنامج «{program}».")
        if program.max_per_year:
            count = AidDisbursement.objects.filter(beneficiary=beneficiary, program=program, docstatus=1,
                                                   posting_date__year=self.posting_date.year).exclude(pk=self.pk).count()
            if count >= program.max_per_year:
                raise ValidationError(f"استنفد {beneficiary.full_name} الحد السنوي لبرنامج «{program}» "
                                      f"({program.max_per_year} مرات في {self.posting_date.year}).")

    def validate_for_submit(self):
        self.calculate_totals()
        self.check_eligibility()
        if self.aid_type == AidType.CASH:
            if not self.amount or self.amount <= 0:
                raise ValidationError("أدخل مبلغ المساعدة النقدية.")
            if not self.payment_account_id:
                raise ValidationError("حدد الصندوق أو البنك الذي تُصرف منه المساعدة.")
        elif self.aid_type == AidType.VOUCHER:
            from beneficiaries.vouchers import validate_issue

            validate_issue(self)
        else:
            if not self.warehouse_id:
                raise ValidationError("حدد المستودع الذي تُصرف منه المساعدة العينية.")
            rows = list(self.items.select_related("item"))
            if not rows:
                raise ValidationError("أضف أصناف المساعدة العينية.")
            for row in rows:
                if not row.item.is_stock_item or row.qty <= 0:
                    raise ValidationError(f"الصنف {row.item} يجب أن يكون مخزنياً بكمية أكبر من صفر.")
        from beneficiaries.committee import check_before_submit

        check_before_submit(self)
        # القسيمة تصل المستفيد برسالة، ويتحقق المتجر من هويته عند الشراء؛ لا تسليم يدوي يُؤكَّد
        if self.company.require_aid_otp and self.aid_type != AidType.VOUCHER:
            if not self.received_at:
                raise ValidationError("أكّد استلام المستفيد برمز التحقق المرسل إلى جواله قبل الاعتماد.")
            if self.receipt_fingerprint != self.content_fingerprint():
                raise ValidationError("تغيّرت بيانات المساعدة بعد تأكيد الاستلام. أعد إرسال رمز التحقق وتأكيده.")

    def on_submit(self):
        from accounts.ledger import make_gl_entries

        remarks = f"{self.program} — {self.beneficiary.full_name}"
        if self.aid_type == AidType.VOUCHER:
            from beneficiaries.vouchers import issue_voucher

            issue_voucher(self, remarks)
            return
        if self.aid_type == AidType.CASH:
            make_gl_entries(self, [{"account": self.program.expense_account, "debit": self.amount, "fund": self.fund},
                                   {"account": self.payment_account, "credit": self.amount}], remarks=remarks)
            return
        from stock.ledger import make_stock_ledger_entry

        total = flt(0)
        for row in self.items.select_related("item"):
            value = -make_stock_ledger_entry(self, row.item, self.warehouse, -row.qty)
            row.amount = flt(value)
            row.save(update_fields=["amount"])
            total += row.amount
        self.amount = total
        self.save(update_fields=["amount"])
        if total:
            make_gl_entries(self, [
                {"account": self.program.expense_account, "debit": total, "fund": self.fund},
                {"account": self.company.require_account("default_stock_account"), "credit": total},
            ], remarks=remarks)

    def before_cancel(self):
        if self.aid_type == AidType.VOUCHER:
            from beneficiaries.vouchers import void_voucher

            void_voucher(self)

    def on_cancel(self):
        from accounts.ledger import cancel_gl_entries
        from stock.ledger import cancel_stock_ledger_entries

        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)

    # ── تأكيد الاستلام ──
    def content_fingerprint(self):
        """بصمة ما أكد المستفيد استلامه: أي تعديل بعد التأكيد يُبطله."""
        import hashlib

        items = sorted((row.item_id, str(flt(row.qty, 3))) for row in self.items.all())
        content = [self.beneficiary_id, self.program_id, str(flt(self.amount)), self.warehouse_id, items]
        return hashlib.sha256(repr(content).encode()).hexdigest()

    def _hash(self, code):
        import hashlib

        return hashlib.sha256(f"{settings.SECRET_KEY}:{self.pk}:{code}".encode()).hexdigest()

    def issue_otp(self):
        """يولّد رمزاً من 6 أرقام ويحفظ بصمته فقط. يعيد الرمز لإرساله."""
        import secrets

        if not self.is_draft:
            raise ValidationError("رمز التحقق للمسودات فقط.")
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.otp_hash, self.otp_sent_at, self.otp_attempts = self._hash(code), timezone.now(), 0
        self.save(update_fields=["otp_hash", "otp_sent_at", "otp_attempts"])
        return code

    def verify_otp(self, code, user):
        import hmac

        if not self.is_draft or self.received_at:
            raise ValidationError("تم تأكيد الاستلام مسبقاً.")
        if not self.otp_hash:
            raise ValidationError("أرسل رمز التحقق أولاً.")
        if self.otp_attempts >= OTP_MAX_ATTEMPTS:
            raise ValidationError("تجاوزت عدد المحاولات. أرسل رمزاً جديداً.")
        if timezone.now() - self.otp_sent_at > timezone.timedelta(minutes=OTP_TTL_MINUTES):
            raise ValidationError("انتهت صلاحية الرمز. أرسل رمزاً جديداً.")
        if not hmac.compare_digest(self._hash((code or "").strip()), self.otp_hash):
            self.otp_attempts += 1
            self.save(update_fields=["otp_attempts"])
            raise ValidationError("رمز التحقق غير صحيح.")
        self._mark_received(ReceiptMethod.OTP, user)

    def confirm_manually(self, user, note):
        if not self.is_draft or self.received_at:
            raise ValidationError("تم تأكيد الاستلام مسبقاً.")
        if not (note or "").strip():
            raise ValidationError("اذكر سبب التأكيد اليدوي (مثل: المستفيد بلا جوال، وتم التحقق من هويته).")
        self.receipt_note = note.strip()[:255]
        self._mark_received(ReceiptMethod.MANUAL, user)

    def _mark_received(self, method, user):
        self.calculate_totals()
        self.received_at, self.receipt_method, self.confirmed_by, self.otp_hash = timezone.now(), method, user, ""
        self.receipt_fingerprint = self.content_fingerprint()
        self.save()


class AidDisbursementItem(models.Model):
    parent = models.ForeignKey(AidDisbursement, models.CASCADE, related_name="items")
    item = models.ForeignKey("stock.Item", models.PROTECT, limit_choices_to={"disabled": False, "is_stock_item": True},
                             verbose_name="الصنف")
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    amount = models.DecimalField("التكلفة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "صنف مساعدة"
        verbose_name_plural = "أصناف المساعدة"
        ordering = ["id"]


# ───────────────────────────── لجنة المساعدات ─────────────────────────────

class CommitteeRule(models.TextChoices):
    MAJORITY = "majority", "أغلبية الأصوات"
    UNANIMOUS = "unanimous", "إجماع الأصوات"


class AidCommittee(TenantModel):
    """لجنة المساعدات: تعرض عليها دراسات الحالة والمساعدات الكبيرة، ويصوت أعضاؤها عن بُعد من حساباتهم."""

    name = models.CharField("اسم اللجنة", max_length=100, default="لجنة المساعدات")
    is_active = models.BooleanField("مفعّلة", default=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="+", verbose_name="أعضاء اللجنة")
    quorum = models.PositiveSmallIntegerField("النصاب", default=3,
                                              help_text="أقل عدد من الأصوات يصدر به القرار")
    decision_rule = models.CharField("طريقة القرار", max_length=10, choices=CommitteeRule.choices,
                                     default=CommitteeRule.MAJORITY)
    review_assessments = models.BooleanField("تُعرض دراسات الحالة على اللجنة", default=True,
                                             help_text="قرار اللجنة هو الذي يحدد استحقاق المستفيد")
    disbursement_threshold = models.DecimalField(
        "تُعرض المساعدات التي تبلغ قيمتها", max_digits=14, decimal_places=2, default=0,
        help_text="صفر = لا تُعرض المساعدات على اللجنة")

    class Meta:
        verbose_name = "لجنة المساعدات"
        verbose_name_plural = "لجنة المساعدات"
        constraints = [models.UniqueConstraint(fields=["company"], name="one_aid_committee_per_company")]

    def __str__(self):
        return self.name

    def clean(self):
        if not self.quorum:
            raise ValidationError({"quorum": "النصاب صوت واحد على الأقل."})

    @classmethod
    def active_for(cls, company):
        return cls.all_objects.filter(company=company, is_active=True).first()


class CaseStatus(models.TextChoices):
    PENDING = "pending", "بانتظار قرار اللجنة"
    APPROVED = "approved", "وافقت اللجنة"
    REJECTED = "rejected", "رفضت اللجنة"


class CommitteeCase(TenantModel):
    """عرض مستند (دراسة حالة أو صرف مساعدة) على اللجنة وقرارها فيه."""

    committee = models.ForeignKey(AidCommittee, models.CASCADE, related_name="cases", verbose_name="اللجنة")
    doctype = models.CharField("نوع المستند", max_length=50)
    object_id = models.PositiveBigIntegerField("المعرّف")
    title = models.CharField("الموضوع", max_length=255)
    amount = models.DecimalField("القيمة", max_digits=14, decimal_places=2, default=0)
    status = models.CharField("القرار", max_length=10, choices=CaseStatus.choices, default=CaseStatus.PENDING)
    fingerprint = models.CharField(max_length=64, editable=False)
    referred_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, related_name="+",
                                    verbose_name="عرضها")
    referred_at = models.DateTimeField("تاريخ العرض", auto_now_add=True)
    decided_at = models.DateTimeField("تاريخ القرار", null=True, blank=True)

    class Meta:
        verbose_name = "عرض على لجنة المساعدات"
        verbose_name_plural = "قرارات لجنة المساعدات"
        ordering = ["-referred_at"]
        constraints = [models.UniqueConstraint(fields=["company", "doctype", "object_id"],
                                               name="one_committee_case_per_document")]

    def __str__(self):
        return self.title


class VoteChoice(models.TextChoices):
    APPROVE = "approve", "موافق"
    REJECT = "reject", "غير موافق"


class CommitteeVote(models.Model):
    case = models.ForeignKey(CommitteeCase, models.CASCADE, related_name="votes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, related_name="+", verbose_name="العضو")
    vote = models.CharField("الصوت", max_length=10, choices=VoteChoice.choices)
    note = models.CharField("الملاحظة", max_length=255, blank=True)
    voted_at = models.DateTimeField("الوقت", auto_now=True)

    class Meta:
        verbose_name = "صوت عضو"
        verbose_name_plural = "أصوات الأعضاء"
        ordering = ["voted_at"]
        constraints = [models.UniqueConstraint(fields=["case", "user"], name="one_vote_per_member")]


# ───────────────────────────── القسائم الشرائية ─────────────────────────────

class VoucherMerchant(TenantModel):
    """متجر شريك يقبل القسائم الشرائية للمستفيدين؛ يستخدمها من صفحة برابط سري خاص به."""

    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, related_name="+", verbose_name="المورد (المتجر)",
                                 help_text="تُقيد قيمة القسائم المستخدمة مستحقةً له في حسابه")
    name = models.CharField("اسم الفرع / المتجر", max_length=150, blank=True,
                            help_text="فارغ = اسم المورد")
    phone = models.CharField("جوال المسؤول", max_length=20, blank=True)
    token = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField("مفعّل", default=True)

    class Meta:
        verbose_name = "متجر شريك"
        verbose_name_plural = "المتاجر الشريكة للقسائم"
        ordering = ["name"]

    def __str__(self):
        return self.name or str(self.supplier)

    def save(self, *args, **kwargs):
        import secrets

        if not self.token:
            self.token = secrets.token_urlsafe(32)
        if not self.name and self.supplier_id:
            self.name = self.supplier.name
        super().save(*args, **kwargs)

    def regenerate_token(self):
        import secrets

        self.token = secrets.token_urlsafe(32)
        self.save(update_fields=["token"])


class VoucherStatus(models.TextChoices):
    ACTIVE = "active", "سارية"
    USED = "used", "مستخدمة بالكامل"
    EXPIRED = "expired", "منتهية"
    VOID = "void", "ملغاة"


VOUCHER_MAX_ATTEMPTS = 5


class AidVoucher(TenantModel):
    """قسيمة شرائية تصدر عند اعتماد صرف مساعدة من نوع «قسيمة»، ويخصم منها المتجر الشريك حتى تنفد أو تنتهي."""

    disbursement = models.OneToOneField(AidDisbursement, models.PROTECT, related_name="voucher",
                                        verbose_name="صرف المساعدة")
    beneficiary = models.ForeignKey(Beneficiary, models.PROTECT, related_name="vouchers", verbose_name="المستفيد")
    code = models.CharField("رقم القسيمة", max_length=12, unique=True, editable=False)
    amount = models.DecimalField("القيمة", max_digits=14, decimal_places=2)
    balance = models.DecimalField("الرصيد المتبقي", max_digits=14, decimal_places=2)
    expires_on = models.DateField("صالحة حتى")
    status = models.CharField("الحالة", max_length=10, choices=VoucherStatus.choices, default=VoucherStatus.ACTIVE)
    failed_attempts = models.PositiveSmallIntegerField("محاولات تحقق خاطئة", default=0)
    created_at = models.DateTimeField("تاريخ الإصدار", auto_now_add=True)

    class Meta:
        verbose_name = "قسيمة شرائية"
        verbose_name_plural = "القسائم الشرائية"
        ordering = ["-id"]

    def __str__(self):
        return f"قسيمة {self.code}"

    @property
    def is_locked(self):
        return self.failed_attempts >= VOUCHER_MAX_ATTEMPTS

    @property
    def used(self):
        return flt(self.amount - self.balance) if self.status != VoucherStatus.VOID else flt(0)

    @classmethod
    def new_code(cls):
        import secrets

        while True:
            code = "".join(str(secrets.randbelow(10)) for _ in range(12))
            if code[0] != "0" and not cls.all_objects.filter(code=code).exists():
                return code


class RedemptionKind(models.TextChoices):
    REDEEM = "redeem", "شراء لدى المتجر"
    EXPIRE = "expire", "انتهاء الصلاحية (إرجاع الرصيد)"


class VoucherRedemption(SubmittableDocument):
    """استخدام قسيمة لدى متجر شريك (يصبح المبلغ مستحقاً للمتجر)، أو إرجاع رصيدها المنتهي للبرنامج."""

    naming_prefix = "VRD"

    voucher = models.ForeignKey(AidVoucher, models.PROTECT, related_name="redemptions", verbose_name="القسيمة")
    kind = models.CharField("النوع", max_length=10, choices=RedemptionKind.choices, default=RedemptionKind.REDEEM)
    merchant = models.ForeignKey(VoucherMerchant, models.PROTECT, null=True, blank=True, related_name="redemptions",
                                 verbose_name="المتجر")
    amount = models.DecimalField("المبلغ", max_digits=14, decimal_places=2)
    invoice_no = models.CharField("رقم فاتورة المتجر", max_length=60, blank=True)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "استخدام قسيمة"
        verbose_name_plural = "عمليات القسائم الشرائية"

    def validate_for_submit(self):
        from beneficiaries.vouchers import validate_redemption

        validate_redemption(self)

    def on_submit(self):
        from beneficiaries.vouchers import post_redemption

        post_redemption(self)

    def on_cancel(self):
        from beneficiaries.vouchers import reverse_redemption

        reverse_redemption(self)


class DependentCategory(TenantModel):
    name = models.CharField("تصنيف التابع / حالته", max_length=80)
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "تصنيف تابع"
        verbose_name_plural = "تصنيفات وحالات التابعين"
        constraints = [tenant_unique("name", model_label="dependentcategory")]

    def __str__(self):
        return self.name


class BeneficiaryFollowUp(TenantModel):
    version = models.PositiveIntegerField(default=1, editable=False)

    def save(self,*args,**kwargs):
        from django.db import transaction
        with transaction.atomic():
            if self.pk:
                previous=BeneficiaryFollowUp.objects.select_for_update().get(pk=self.pk)
                self.version=previous.version+1
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"]=set(kwargs["update_fields"])|{"version"}
            return super().save(*args,**kwargs)

    beneficiary = models.ForeignKey(Beneficiary, models.CASCADE, related_name="follow_ups", verbose_name="المستفيد")
    subject = models.CharField("عنوان المتابعة", max_length=200)
    notes = models.TextField("ملاحظات المتابعة", blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, models.PROTECT, related_name="+", verbose_name="المسؤول")
    due_date = models.DateField("موعد المتابعة", null=True, blank=True)
    completed = models.BooleanField("مكتملة", default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "متابعة مستفيد"
        verbose_name_plural = "متابعات المستفيدين"
        ordering = ["completed", "due_date", "-pk"]

    def __str__(self):
        return self.subject

    def clean(self):
        super().clean()
        from core.models import Company, Membership
        company_id=self.company_id or getattr(Company.current(),"pk",None)
        if self.beneficiary_id and self.beneficiary.company_id!=company_id:
            raise ValidationError({"beneficiary":"المستفيد لا يخص هذه الجمعية."})
        if self.assigned_to_id and not Membership.objects.filter(company_id=company_id,user_id=self.assigned_to_id,is_active=True).exists():
            raise ValidationError({"assigned_to":"المسؤول ليس مستخدمًا نشطًا بهذه الجمعية."})


class FieldSubmission(TenantModel):
    follow_up = models.ForeignKey(BeneficiaryFollowUp, models.PROTECT, null=True, related_name="field_submissions")
    doctype = models.CharField(max_length=40,default="beneficiary-follow-up")
    object_id = models.PositiveBigIntegerField(null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, models.PROTECT, related_name="+")
    token = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    result_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company","token"],name="field_submission_once")]
