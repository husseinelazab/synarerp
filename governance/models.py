"""إدارة الأعضاء المشاركين (الجمعية العمومية ومجلس الإدارة والاجتماعات) وإدارة الحوكمة (المعايير والتقييم الذاتي والشواهد)."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from beneficiaries.models import validate_national_id
from core.models import NamingSeries, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


def current_year():
    return timezone.localdate().year


# ───────────────────────────── الأعضاء ─────────────────────────────

class MembershipCategory(TenantModel):
    name = models.CharField("فئة العضوية", max_length=100)
    annual_fee = models.DecimalField("الاشتراك السنوي", max_digits=12, decimal_places=2, default=0)
    has_voting_rights = models.BooleanField("له حق التصويت في الجمعية العمومية", default=True)

    class Meta:
        verbose_name = "فئة عضوية"
        verbose_name_plural = "فئات العضوية"
        ordering = ["id"]
        constraints = [tenant_unique("name", model_label="membershipcategory")]

    def __str__(self):
        return self.name


class MemberStatus(models.TextChoices):
    ACTIVE = "active", "نشط"
    SUSPENDED = "suspended", "موقوف"
    ENDED = "ended", "منتهية عضويته"
    PENDING = "pending", "طلب انضمام"


class Member(TenantModel):
    member_number = models.CharField("رقم العضوية", max_length=30, editable=False)
    full_name = models.CharField("الاسم", max_length=150)
    national_id = models.CharField("رقم الهوية", max_length=10, blank=True, validators=[validate_national_id])
    phone = models.CharField("الجوال", max_length=20, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    category = models.ForeignKey(MembershipCategory, models.PROTECT, related_name="members", verbose_name="فئة العضوية")
    join_date = models.DateField("تاريخ الانضمام", default=timezone.localdate)
    status = models.CharField("الحالة", max_length=10, choices=MemberStatus.choices, default=MemberStatus.ACTIVE)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "عضو"
        verbose_name_plural = "أعضاء الجمعية العمومية"
        ordering = ["member_number"]
        constraints = [tenant_unique("member_number", model_label="member")]

    def __str__(self):
        return f"{self.member_number} — {self.full_name}" if self.member_number else self.full_name

    def save(self, *args, **kwargs):
        if not self.member_number:
            from core.models import Company

            self.member_number = NamingSeries.next_name(self.company if self.company_id else Company.current(), "MEM")
        super().save(*args, **kwargs)

    def fee_settled(self, year):
        if not self.category.annual_fee:
            return True
        return self.fees.filter(year=year, status__in=[FeeStatus.PAID, FeeStatus.WAIVED]).exists()

    def can_vote(self, year):
        """حق التصويت: عضو نشط في فئة لها حق التصويت، ومسدد لاشتراك السنة."""
        return self.status == MemberStatus.ACTIVE and self.category.has_voting_rights and self.fee_settled(year)


class FeeStatus(models.TextChoices):
    PENDING = "pending", "غير مسدد"
    PARTIAL = "partial", "مسدد جزئياً"
    PAID = "paid", "مسدد"
    WAIVED = "waived", "معفى"


class MembershipFee(TenantModel):
    member = models.ForeignKey(Member, models.CASCADE, related_name="fees", verbose_name="العضو")
    year = models.PositiveSmallIntegerField("السنة")
    amount = models.DecimalField("المبلغ", max_digits=12, decimal_places=2)
    collected_amount = models.DecimalField("المسدد", max_digits=12, decimal_places=2, default=0, editable=False)
    status = models.CharField("الحالة", max_length=10, choices=FeeStatus.choices, default=FeeStatus.PENDING,
                              editable=False)

    class Meta:
        verbose_name = "اشتراك عضوية"
        verbose_name_plural = "اشتراكات الأعضاء"
        ordering = ["-year", "member__member_number"]
        constraints = [models.UniqueConstraint(fields=["member", "year"], name="membership_fee_per_year",
                                               violation_error_message="يوجد اشتراك لهذا العضو في نفس السنة.")]

    def __str__(self):
        return f"{self.member.full_name} — {self.year}"

    @property
    def outstanding(self):
        return flt(max(flt(self.amount) - flt(self.collected_amount), 0))

    def refresh_collected(self):
        self.collected_amount = flt(self.receipts.filter(docstatus=1).aggregate(t=models.Sum("amount"))["t"])
        if self.status != FeeStatus.WAIVED:
            self.status = (FeeStatus.PAID if self.collected_amount >= self.amount else
                           FeeStatus.PARTIAL if self.collected_amount else FeeStatus.PENDING)
        self.save(update_fields=["collected_amount", "status"])

    @property
    def available_actions(self):
        if self.status in (FeeStatus.PENDING, FeeStatus.PARTIAL):
            return [("collect", "تسجيل السداد"), ("waive", "إعفاء")]
        return []

    def collect(self, user):
        from funds.models import DonationReceipt, DonationType

        return DonationReceipt.objects.create(
            donor_name=self.member.full_name, donor_phone=self.member.phone, donation_type=DonationType.MEMBERSHIP,
            amount=self.outstanding, account=self.company.default_cash_account, membership_fee=self, created_by=user,
            remarks=f"اشتراك عضوية {self.member.member_number} لعام {self.year}")

    def waive(self, user):
        self.status = FeeStatus.WAIVED
        self.save(update_fields=["status"])


def generate_fees(year):
    """اشتراكات السنة لكل الأعضاء النشطين في فئات باشتراك (لا تتكرر). يعيد عدد ما أُنشئ."""
    created = 0
    for member in Member.objects.filter(status=MemberStatus.ACTIVE, category__annual_fee__gt=0).select_related("category"):
        if not member.fees.filter(year=year).exists():
            MembershipFee.objects.create(member=member, year=year, amount=member.category.annual_fee)
            created += 1
    return created


class BoardTerm(TenantModel):
    """دورة مجلس الإدارة: تُؤرشف الدورات السابقة ويُعتمد أعضاء الدورة السارية."""

    name = models.CharField("الدورة", max_length=100, help_text="مثل: الدورة الخامسة")
    start_date = models.DateField("بداية الدورة")
    end_date = models.DateField("نهاية الدورة")
    approval_reference = models.CharField("مرجع اعتماد المجلس", max_length=100, blank=True,
                                          help_text="رقم وتاريخ خطاب اعتماد المجلس من الجهة المشرفة")

    class Meta:
        verbose_name = "دورة مجلس إدارة"
        verbose_name_plural = "دورات مجلس الإدارة"
        ordering = ["-start_date"]
        constraints = [tenant_unique("name", model_label="boardterm")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": "نهاية الدورة يجب أن تكون بعد بدايتها."})
        if self.start_date and self.end_date:
            overlap = BoardTerm.objects.filter(start_date__lte=self.end_date, end_date__gte=self.start_date)
            if overlap.exclude(pk=self.pk).exists():
                raise ValidationError("تتداخل هذه الدورة مع دورة أخرى.")

    @classmethod
    def current(cls, date=None):
        date = date or timezone.localdate()
        return cls.objects.filter(start_date__lte=date, end_date__gte=date).first()


class BoardPosition(models.TextChoices):
    CHAIR = "chair", "رئيس مجلس الإدارة"
    VICE = "vice", "نائب الرئيس"
    TREASURER = "treasurer", "المشرف المالي"
    SECRETARY = "secretary", "أمين السر"
    MEMBER = "member", "عضو"


class BoardSeat(models.Model):
    parent = models.ForeignKey(BoardTerm, models.CASCADE, related_name="seats")
    member = models.ForeignKey(Member, models.PROTECT, related_name="board_seats", verbose_name="العضو")
    position = models.CharField("المنصب", max_length=10, choices=BoardPosition.choices, default=BoardPosition.MEMBER)

    class Meta:
        verbose_name = "عضو مجلس"
        verbose_name_plural = "أعضاء المجلس"
        ordering = ["id"]


class MeetingType(models.TextChoices):
    GA_ORDINARY = "ga_ordinary", "جمعية عمومية عادية"
    GA_EXTRAORDINARY = "ga_extra", "جمعية عمومية غير عادية"
    BOARD = "board", "مجلس الإدارة"
    COMMITTEE = "committee", "لجنة"


GENERAL_ASSEMBLY = (MeetingType.GA_ORDINARY, MeetingType.GA_EXTRAORDINARY)


class Meeting(SubmittableDocument):
    """اجتماع: الحضور يُجلب تلقائياً، والمحضر لا يُعتمد إلا باكتمال النصاب."""

    naming_prefix = "MTG"
    posts_to_ledger = False
    posting_note = "المحضر معتمد؛ اطبعه بهوية الجمعية من زر الطباعة."

    meeting_type = models.CharField("نوع الاجتماع", max_length=12, choices=MeetingType.choices,
                                    default=MeetingType.BOARD)
    title = models.CharField("عنوان الاجتماع", max_length=200, help_text="مثل: الاجتماع الثالث لعام 2026")
    start_time = models.TimeField("الوقت", null=True, blank=True)
    location = models.CharField("المكان / رابط الاجتماع", max_length=255, blank=True)
    agenda = models.TextField("جدول الأعمال", blank=True)
    quorum_percent = models.PositiveSmallIntegerField(
        "النصاب المطلوب %", default=50, help_text="نسبة الحاضرين ممن يحق لهم الحضور ليصح الاجتماع (حسب اللائحة)")
    minutes = models.TextField("المحضر", blank=True)
    eligible_count = models.PositiveSmallIntegerField("يحق لهم الحضور", default=0, editable=False)
    present_count = models.PositiveSmallIntegerField("الحاضرون", default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "اجتماع"
        verbose_name_plural = "الاجتماعات والمحاضر"

    def __str__(self):
        return f"{self.name} — {self.title}" if self.name else self.title

    def expected_attendees(self):
        if self.meeting_type in GENERAL_ASSEMBLY:
            year = self.posting_date.year
            return [m for m in Member.objects.filter(status=MemberStatus.ACTIVE).select_related("category")
                    if m.can_vote(year)]
        if self.meeting_type == MeetingType.BOARD:
            term = BoardTerm.current(self.posting_date)
            return [seat.member for seat in term.seats.select_related("member")] if term else []
        return []

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft and not self.attendance.exists():
            MeetingAttendance.objects.bulk_create([MeetingAttendance(parent=self, member=m)
                                                   for m in self.expected_attendees()])
        rows = list(self.attendance.all())
        self.eligible_count = len(rows)
        self.present_count = sum(1 for r in rows if r.present)

    @property
    def quorum_met(self):
        return bool(self.eligible_count) and self.present_count * 100 >= self.quorum_percent * self.eligible_count

    def validate_for_submit(self):
        if not self.minutes.strip():
            raise ValidationError("اكتب المحضر قبل اعتماده.")
        if self.meeting_type != MeetingType.COMMITTEE and not self.eligible_count:
            raise ValidationError("لا يوجد من يحق لهم الحضور: أضف الأعضاء أو دورة مجلس الإدارة السارية.")
        if self.eligible_count and not self.quorum_met:
            raise ValidationError(f"لم يكتمل النصاب: حضر {self.present_count} من {self.eligible_count} "
                                  f"والمطلوب {self.quorum_percent}%.")


class AttendanceMode(models.TextChoices):
    IN_PERSON = "in_person", "حضوري"
    REMOTE = "remote", "عن بعد"
    PROXY = "proxy", "بالإنابة"


class MeetingAttendance(models.Model):
    parent = models.ForeignKey(Meeting, models.CASCADE, related_name="attendance")
    member = models.ForeignKey(Member, models.PROTECT, related_name="+", verbose_name="العضو")
    present = models.BooleanField("حاضر", default=False)
    mode = models.CharField("طريقة الحضور", max_length=10, choices=AttendanceMode.choices,
                            default=AttendanceMode.IN_PERSON)
    note = models.CharField("ملاحظة", max_length=150, blank=True)

    class Meta:
        verbose_name = "حضور"
        verbose_name_plural = "الحضور"
        ordering = ["id"]


class DecisionStatus(models.TextChoices):
    OPEN = "open", "قيد التنفيذ"
    DONE = "done", "منفّذ"
    CANCELLED = "cancelled", "ملغى"


class MeetingDecision(TenantModel):
    meeting = models.ForeignKey(Meeting, models.CASCADE, related_name="decisions", verbose_name="الاجتماع")
    text = models.TextField("القرار")
    responsible = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="المسؤول عن التنفيذ")
    due_date = models.DateField("موعد التنفيذ", null=True, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=DecisionStatus.choices, default=DecisionStatus.OPEN)

    class Meta:
        verbose_name = "قرار"
        verbose_name_plural = "القرارات ومتابعة تنفيذها"
        ordering = ["status", "due_date", "id"]

    def __str__(self):
        return self.text[:80]

    @property
    def is_overdue(self):
        return self.status == DecisionStatus.OPEN and self.due_date and self.due_date < timezone.localdate()


# ───────────────────────────── الحوكمة ─────────────────────────────

class GovernanceStandard(TenantModel):
    code = models.CharField("الرمز", max_length=10)
    name = models.CharField("المعيار", max_length=150)

    class Meta:
        verbose_name = "معيار حوكمة"
        verbose_name_plural = "معايير الحوكمة"
        ordering = ["code"]
        constraints = [tenant_unique("code", model_label="governancestandard")]

    def __str__(self):
        return f"{self.code} {self.name}"


class AutoCheck(models.TextChoices):
    NONE = "", "يدوي"
    GA_HELD = "ga_held", "عقد جمعية عمومية عادية معتمدة المحضر خلال السنة"
    BOARD_MEETINGS = "board_meetings", "عدد اجتماعات مجلس الإدارة المعتمدة خلال السنة"
    BOARD_TERM = "board_term", "وجود دورة مجلس إدارة سارية"
    DECISIONS = "decisions", "عدم وجود قرارات متأخرة التنفيذ"
    CASE_STUDIES = "case_studies", "نسبة المستفيدين المعتمدين بدراسة حالة سارية"
    FUNDS = "funds", "عدم وجود عجز في الصناديق المقيدة"


class GovernanceIndicator(TenantModel):
    standard = models.ForeignKey(GovernanceStandard, models.PROTECT, related_name="indicators", verbose_name="المعيار")
    code = models.CharField("الرمز", max_length=15)
    text = models.TextField("المؤشر / الممارسة")
    weight = models.DecimalField("الوزن (الدرجة)", max_digits=6, decimal_places=2, default=1)
    evidence_hint = models.CharField("الشواهد المطلوبة", max_length=255, blank=True)
    auto_check = models.CharField("التحقق الآلي من بيانات النظام", max_length=20, choices=AutoCheck.choices,
                                  blank=True)
    auto_target = models.PositiveSmallIntegerField("الحد المطلوب للتحقق الآلي", default=0,
                                                   help_text="مثل عدد اجتماعات المجلس أو النسبة المئوية")
    is_active = models.BooleanField("مفعّل", default=True)

    class Meta:
        verbose_name = "مؤشر حوكمة"
        verbose_name_plural = "مؤشرات الحوكمة"
        ordering = ["standard__code", "code"]
        constraints = [tenant_unique("code", model_label="governanceindicator")]

    def __str__(self):
        return f"{self.code} {self.text[:70]}"

    def evaluate(self, year):
        """للمؤشر الآلي: (محقق؟، وصف النتيجة). None للمؤشر اليدوي."""
        if not self.auto_check:
            return None
        return AUTO_CHECKS[self.auto_check](year, self.auto_target)


def _ga_held(year, target):
    count = Meeting.objects.filter(docstatus=1, meeting_type=MeetingType.GA_ORDINARY, posting_date__year=year).count()
    return count >= max(target, 1), f"اجتماعات جمعية عمومية عادية معتمدة: {count}"


def _board_meetings(year, target):
    count = Meeting.objects.filter(docstatus=1, meeting_type=MeetingType.BOARD, posting_date__year=year).count()
    return count >= max(target, 1), f"اجتماعات مجلس إدارة معتمدة: {count} (المطلوب {max(target, 1)})"


def _board_term(year, target):
    term = BoardTerm.current()
    return term is not None and term.seats.exists(), (f"الدورة السارية: {term} حتى {term.end_date:%Y-%m-%d}"
                                                      if term else "لا توجد دورة سارية")


def _decisions(year, target):
    late = [d for d in MeetingDecision.objects.filter(status=DecisionStatus.OPEN) if d.is_overdue]
    return not late, f"قرارات متأخرة التنفيذ: {len(late)}"


def _case_studies(year, target):
    from beneficiaries.models import Beneficiary, BeneficiaryStatus

    approved = Beneficiary.objects.filter(status=BeneficiaryStatus.APPROVED)
    total = approved.count()
    valid = approved.filter(next_review_date__gt=timezone.localdate()).count()
    ratio = round(valid * 100 / total) if total else 100
    return ratio >= (target or 100), f"دراسات سارية: {valid} من {total} ({ratio}%)"


def _funds(year, target):
    from accounts.models import Fund, FundType

    short = [f.name for f in Fund.objects.filter(fund_type=FundType.RESTRICTED) if f.balance < 0]
    return not short, "صناديق بعجز: " + ("، ".join(short) if short else "لا يوجد")


AUTO_CHECKS = {AutoCheck.GA_HELD: _ga_held, AutoCheck.BOARD_MEETINGS: _board_meetings,
               AutoCheck.BOARD_TERM: _board_term, AutoCheck.DECISIONS: _decisions,
               AutoCheck.CASE_STUDIES: _case_studies, AutoCheck.FUNDS: _funds}


class ScoreStatus(models.TextChoices):
    MET = "met", "محقق"
    PARTIAL = "partial", "محقق جزئياً"
    NOT_MET = "not_met", "غير محقق"
    NA = "na", "لا ينطبق"


SCORE_FACTOR = {ScoreStatus.MET: Decimal(1), ScoreStatus.PARTIAL: Decimal("0.5"), ScoreStatus.NOT_MET: Decimal(0)}


class GovernanceAssessment(SubmittableDocument):
    """التقييم الذاتي السنوي للحوكمة قبل زيارة فريق الحوكمة."""

    naming_prefix = "GOV"
    posts_to_ledger = False
    posting_note = "التقييم معتمد؛ يمكن طباعته ومقارنته بنتيجة زيارة فريق الحوكمة."

    year = models.PositiveSmallIntegerField("السنة", default=current_year)
    notes = models.TextField("ملاحظات وخطة التحسين", blank=True)
    total_weight = models.DecimalField("مجموع الأوزان", max_digits=8, decimal_places=2, default=0, editable=False)
    total_score = models.DecimalField("الدرجة", max_digits=8, decimal_places=2, default=0, editable=False)
    score_percent = models.DecimalField("النسبة %", max_digits=5, decimal_places=1, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "تقييم ذاتي للحوكمة"
        verbose_name_plural = "التقييم الذاتي للحوكمة"

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft and not self.scores.exists():
            GovernanceScore.objects.bulk_create([
                GovernanceScore(parent=self, indicator=i, weight=i.weight)
                for i in GovernanceIndicator.objects.filter(is_active=True).select_related("standard")])
        if self.is_draft:
            for row in self.scores.select_related("indicator"):
                result = row.indicator.evaluate(self.year)
                if result is not None:
                    met, text = result
                    row.status, row.auto_result = (ScoreStatus.MET if met else ScoreStatus.NOT_MET), text
                    row.save(update_fields=["status", "auto_result"])
        rows = [r for r in self.scores.all() if r.status != ScoreStatus.NA]
        self.total_weight = sum((r.weight for r in rows), Decimal(0))
        self.total_score = sum((r.weight * SCORE_FACTOR.get(r.status, 0) for r in rows if r.status), Decimal(0))
        self.score_percent = (self.total_score * 100 / self.total_weight).quantize(Decimal("0.1")) \
            if self.total_weight else Decimal(0)

    def validate_for_submit(self):
        missing = [r.indicator.code for r in self.scores.select_related("indicator") if not r.status]
        if missing:
            raise ValidationError("حدد حالة كل مؤشر: " + "، ".join(missing))

    def by_standard(self):
        result = {}
        for row in self.scores.select_related("indicator__standard"):
            entry = result.setdefault(row.indicator.standard, {"weight": Decimal(0), "score": Decimal(0)})
            if row.status and row.status != ScoreStatus.NA:
                entry["weight"] += row.weight
                entry["score"] += row.weight * SCORE_FACTOR[row.status]
        return [{"standard": s, **v, "percent": (v["score"] * 100 / v["weight"]).quantize(Decimal("0.1"))
                 if v["weight"] else None} for s, v in result.items()]


class GovernanceScore(models.Model):
    parent = models.ForeignKey(GovernanceAssessment, models.CASCADE, related_name="scores")
    indicator = models.ForeignKey(GovernanceIndicator, models.PROTECT, related_name="+", verbose_name="المؤشر")
    weight = models.DecimalField("الوزن", max_digits=6, decimal_places=2, default=1)
    status = models.CharField("الحالة", max_length=10, choices=ScoreStatus.choices, blank=True)
    auto_result = models.CharField("نتيجة التحقق الآلي", max_length=255, blank=True)
    note = models.CharField("الشواهد / الملاحظات", max_length=255, blank=True)

    class Meta:
        verbose_name = "مؤشر في التقييم"
        verbose_name_plural = "المؤشرات"
        ordering = ["indicator__standard__code", "indicator__code"]


MAX_EVIDENCE_BYTES = 5 * 1024 * 1024


class GovernanceEvidence(TenantModel):
    """شاهد على مؤشر: ملف (حتى 5 ميجابايت) أو رابط."""

    indicator = models.ForeignKey(GovernanceIndicator, models.CASCADE, related_name="evidence", verbose_name="المؤشر")
    year = models.PositiveSmallIntegerField("السنة", default=current_year)
    title = models.CharField("الشاهد", max_length=200)
    link = models.URLField("رابط", blank=True)
    file_name = models.CharField("اسم الملف", max_length=255, blank=True, editable=False)
    content_type = models.CharField(max_length=100, blank=True, editable=False)
    content = models.BinaryField(null=True, blank=True, editable=False)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                    related_name="+", editable=False, verbose_name="رفعه")
    uploaded_at = models.DateTimeField("تاريخ الرفع", auto_now_add=True)

    class Meta:
        verbose_name = "شاهد"
        verbose_name_plural = "الشواهد"
        ordering = ["-year", "-id"]

    def __str__(self):
        return self.title
