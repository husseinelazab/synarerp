"""إدارة التميز المؤسسي: الخطة الاستراتيجية وأهدافها، الخطة التشغيلية (المبادرات والمشاريع)، ومؤشرات الأداء وقراءاتها."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.models import TenantModel

HUNDRED = Decimal(100)
ACHIEVEMENT_CAP = Decimal(150)  # لا يُعرض إنجاز يتجاوز 150% حتى لا يشوّه المتوسطات
GREEN_FROM = 90
YELLOW_FROM = 70


def _round(value):
    return Decimal(value).quantize(Decimal("0.1"))


def weighted_average(pairs):
    """متوسط مرجّح لأزواج (القيمة، الوزن)؛ صفر إن لم توجد أوزان."""
    total_weight = sum((Decimal(w) for _, w in pairs), Decimal(0))
    if not total_weight:
        return Decimal(0)
    return _round(sum((Decimal(v) * Decimal(w) for v, w in pairs), Decimal(0)) / total_weight)


# ───────────────────────────── الخطة الاستراتيجية ─────────────────────────────

class PlanStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    APPROVED = "approved", "معتمدة"
    ENDED = "ended", "منتهية"


class StrategicPlan(TenantModel):
    title = models.CharField("عنوان الخطة", max_length=200, help_text="مثل: الخطة الاستراتيجية 2026–2030")
    start_year = models.PositiveSmallIntegerField("سنة البداية")
    end_year = models.PositiveSmallIntegerField("سنة النهاية")
    vision = models.TextField("الرؤية", blank=True)
    mission = models.TextField("الرسالة", blank=True)
    values = models.TextField("القيم", blank=True, help_text="قيمة في كل سطر")
    status = models.CharField("الحالة", max_length=10, choices=PlanStatus.choices, default=PlanStatus.DRAFT,
                              editable=False)

    class Meta:
        verbose_name = "خطة استراتيجية"
        verbose_name_plural = "الخطط الاستراتيجية"
        ordering = ["-start_year", "-id"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_year and self.end_year and self.end_year < self.start_year:
            raise ValidationError({"end_year": "سنة النهاية يجب ألا تسبق سنة البداية."})
        if self.start_year and self.end_year and self.end_year - self.start_year > 10:
            raise ValidationError({"end_year": "مدة الخطة الاستراتيجية لا تتجاوز عشر سنوات."})
        if self.status == PlanStatus.APPROVED:
            self.check_overlap()

    def check_overlap(self):
        """لا تُعتمد خطتان تتداخل سنواتهما."""
        overlap = StrategicPlan.objects.filter(status=PlanStatus.APPROVED, start_year__lte=self.end_year,
                                               end_year__gte=self.start_year).exclude(pk=self.pk).first()
        if overlap:
            raise ValidationError(f"تتداخل سنوات هذه الخطة مع الخطة المعتمدة «{overlap}». أنهِ تلك الخطة أولاً.")

    @classmethod
    def current(cls):
        """الخطة المعتمدة السارية هذا العام، أو آخر خطة معتمدة، أو أحدث خطة."""
        year = timezone.localdate().year
        approved = cls.objects.filter(status=PlanStatus.APPROVED)
        return (approved.filter(start_year__lte=year, end_year__gte=year).first() or approved.first()
                or cls.objects.first())

    @property
    def values_list(self):
        return [line.strip() for line in self.values.splitlines() if line.strip()]

    @property
    def progress(self):
        """متوسط تقدم الأهداف مرجّحاً بأوزانها."""
        goals = list(self.goals.all())
        return weighted_average([(g.progress, g.effective_weight) for g in goals])

    @property
    def available_actions(self):
        if self.status == PlanStatus.DRAFT:
            return [("approve", "اعتماد الخطة")]
        if self.status == PlanStatus.APPROVED:
            return [("close", "إنهاء الخطة")]
        return []

    def approve(self, user):
        if not self.goals.exists():
            raise ValidationError("أضف أهداف الخطة قبل اعتمادها.")
        self.check_overlap()
        self.status = PlanStatus.APPROVED
        self.save(update_fields=["status"])

    def close(self, user):
        self.status = PlanStatus.ENDED
        self.save(update_fields=["status"])


class Vision2030Pillar(models.TextChoices):
    VIBRANT_SOCIETY = "society", "مجتمع حيوي"
    THRIVING_ECONOMY = "economy", "اقتصاد مزدهر"
    AMBITIOUS_NATION = "nation", "وطن طموح"


SDG_NAMES = [
    "القضاء على الفقر", "القضاء التام على الجوع", "الصحة الجيدة والرفاه", "التعليم الجيد",
    "المساواة بين الجنسين", "المياه النظيفة والنظافة الصحية", "طاقة نظيفة وبأسعار معقولة",
    "العمل اللائق ونمو الاقتصاد", "الصناعة والابتكار والهياكل الأساسية", "الحد من أوجه عدم المساواة",
    "مدن ومجتمعات محلية مستدامة", "الاستهلاك والإنتاج المسؤولان", "العمل المناخي", "الحياة تحت الماء",
    "الحياة في البر", "السلام والعدل والمؤسسات القوية", "عقد الشراكات لتحقيق الأهداف",
]
SDG_CHOICES = [(i, f"{i}. {name}") for i, name in enumerate(SDG_NAMES, start=1)]


class StrategicGoal(TenantModel):
    plan = models.ForeignKey(StrategicPlan, models.CASCADE, related_name="goals", verbose_name="الخطة الاستراتيجية")
    code = models.CharField("الرمز", max_length=20, help_text="مثل: ه1")
    title = models.CharField("الهدف الاستراتيجي", max_length=255)
    description = models.TextField("الوصف", blank=True)
    weight = models.DecimalField("الوزن النسبي %", max_digits=5, decimal_places=2, null=True, blank=True,
                                 validators=[MinValueValidator(0), MaxValueValidator(100)],
                                 help_text="فارغ = يُوزَّع المتبقي من 100% بالتساوي على الأهداف بلا وزن")
    vision2030_pillar = models.CharField("محور رؤية المملكة 2030", max_length=10, choices=Vision2030Pillar.choices,
                                         blank=True)
    sdg = models.PositiveSmallIntegerField("هدف التنمية المستدامة", choices=SDG_CHOICES, null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                              verbose_name="المسؤول")

    class Meta:
        verbose_name = "هدف استراتيجي"
        verbose_name_plural = "الأهداف الاستراتيجية"
        ordering = ["plan", "code", "id"]
        constraints = [models.UniqueConstraint(fields=["plan", "code"], name="strategic_goal_code_per_plan",
                                               violation_error_message="يوجد هدف بنفس الرمز في هذه الخطة.")]

    def __str__(self):
        return f"{self.code} — {self.title}"

    def clean(self):
        if self.plan_id and self.weight:
            others = (StrategicGoal.objects.filter(plan_id=self.plan_id).exclude(pk=self.pk)
                      .aggregate(t=models.Sum("weight"))["t"] or 0)
            if others + self.weight > HUNDRED:
                raise ValidationError({"weight": f"مجموع أوزان أهداف الخطة يتجاوز 100% (المتاح {HUNDRED - others}%)."})

    @property
    def effective_weight(self):
        """الوزن المحدد، أو حصة متساوية مما تبقى من 100% بين الأهداف التي لم يُحدد لها وزن."""
        if self.weight is not None:
            return self.weight
        weights = list(self.plan.goals.values_list("weight", flat=True))
        unweighted = sum(1 for w in weights if w is None) or 1
        remaining = max(HUNDRED - sum((w for w in weights if w is not None), Decimal(0)), Decimal(0))
        return _round(remaining / unweighted)

    @property
    def active_initiatives(self):
        return [i for i in self.initiatives.all() if i.status != InitiativeStatus.CANCELLED]

    @property
    def progress(self):
        """متوسط تقدم مبادرات الهدف (عدا الملغاة) مرجّحاً بأوزانها."""
        return weighted_average([(i.progress, i.weight) for i in self.active_initiatives])


# ───────────────────────────── الخطة التشغيلية ─────────────────────────────

class InitiativeStatus(models.TextChoices):
    NOT_STARTED = "not_started", "لم تبدأ"
    IN_PROGRESS = "in_progress", "جارية"
    COMPLETED = "completed", "مكتملة"
    DELAYED = "delayed", "متأخرة"
    CANCELLED = "cancelled", "ملغاة"


class Initiative(TenantModel):
    goal = models.ForeignKey(StrategicGoal, models.CASCADE, related_name="initiatives", verbose_name="الهدف الاستراتيجي")
    title = models.CharField("المبادرة / المشروع", max_length=255)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                              verbose_name="المسؤول")
    department = models.CharField("الإدارة المنفذة", max_length=100, blank=True)
    start_date = models.DateField("تاريخ البداية")
    end_date = models.DateField("تاريخ النهاية")
    budget = models.DecimalField("الميزانية", max_digits=14, decimal_places=2, default=0)
    actual_cost = models.DecimalField("التكلفة الفعلية", max_digits=14, decimal_places=2, default=0)
    progress = models.PositiveSmallIntegerField("نسبة الإنجاز %", default=0, validators=[MaxValueValidator(100)])
    weight = models.DecimalField("الوزن داخل الهدف", max_digits=5, decimal_places=2, default=1,
                                 validators=[MinValueValidator(0)],
                                 help_text="يُرجَّح به تقدم المبادرة عند حساب تقدم الهدف (1 = أوزان متساوية)")
    status = models.CharField("الحالة", max_length=12, choices=InitiativeStatus.choices,
                              default=InitiativeStatus.NOT_STARTED)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "مبادرة / مشروع تشغيلي"
        verbose_name_plural = "الخطة التشغيلية (المبادرات والمشاريع)"
        ordering = ["end_date", "id"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ النهاية يجب أن يكون بعد البداية."})
        if (self.budget is not None and self.budget < 0) or (self.actual_cost is not None and self.actual_cost < 0):
            raise ValidationError("الميزانية والتكلفة لا تكونان سالبتين.")

    def save(self, *args, **kwargs):
        # اكتمال الإنجاز يكمل المبادرة، وبدء الإنجاز ينقلها إلى «جارية»
        if self.progress >= 100 and self.status != InitiativeStatus.CANCELLED:
            self.status = InitiativeStatus.COMPLETED
        elif self.progress and self.status == InitiativeStatus.NOT_STARTED:
            self.status = InitiativeStatus.IN_PROGRESS
        super().save(*args, **kwargs)

    @property
    def is_delayed(self):
        return (self.end_date < timezone.localdate() and self.progress < 100
                and self.status not in (InitiativeStatus.CANCELLED, InitiativeStatus.COMPLETED))

    @property
    def cost_variance(self):
        return (self.budget or 0) - (self.actual_cost or 0)


# ───────────────────────────── مؤشرات الأداء ─────────────────────────────

class KPIUnit(models.TextChoices):
    PERCENT = "percent", "%"
    COUNT = "count", "عدد"
    SAR = "sar", "ريال"
    HOURS = "hours", "ساعة"
    DAYS = "days", "يوم"
    SCORE = "score", "درجة"


class KPIDirection(models.TextChoices):
    HIGHER = "higher", "الأعلى أفضل"
    LOWER = "lower", "الأقل أفضل"


class KPIFrequency(models.TextChoices):
    MONTHLY = "monthly", "شهري"
    QUARTERLY = "quarterly", "ربع سنوي"
    SEMIANNUAL = "semiannual", "نصف سنوي"
    ANNUAL = "annual", "سنوي"


# الأيام التي إن مرت دون قراءة اعتُبر المؤشر متأخر القياس (دورة القياس + مهلة للإدخال)
STALE_DAYS = {KPIFrequency.MONTHLY: 40, KPIFrequency.QUARTERLY: 100, KPIFrequency.SEMIANNUAL: 200,
              KPIFrequency.ANNUAL: 380}


class KPIStatus(models.TextChoices):
    GREEN = "green", "على المسار"
    YELLOW = "yellow", "يحتاج متابعة"
    RED = "red", "متعثر"
    GREY = "grey", "لا توجد قراءة"


def achievement_percent(value, target, direction):
    """نسبة تحقق المستهدف: الأعلى أفضل = القيمة ÷ المستهدف، والأقل أفضل = المستهدف ÷ القيمة (بحد أقصى 150%)."""
    if value is None or target is None:
        return None
    value, target = Decimal(value), Decimal(target)
    if direction == KPIDirection.LOWER:
        if value <= 0:
            result = HUNDRED if target <= 0 else ACHIEVEMENT_CAP
        elif target <= 0:
            result = Decimal(0)
        else:
            result = target / value * HUNDRED
    elif target == 0:
        result = HUNDRED if value >= 0 else Decimal(0)
    else:
        result = value / target * HUNDRED
    return _round(min(max(result, Decimal(0)), ACHIEVEMENT_CAP))


def achievement_status(achievement):
    if achievement is None:
        return KPIStatus.GREY
    if achievement >= GREEN_FROM:
        return KPIStatus.GREEN
    if achievement >= YELLOW_FROM:
        return KPIStatus.YELLOW
    return KPIStatus.RED


class KPI(TenantModel):
    goal = models.ForeignKey(StrategicGoal, models.CASCADE, related_name="kpis", verbose_name="الهدف الاستراتيجي")
    name = models.CharField("المؤشر", max_length=255)
    unit = models.CharField("وحدة القياس", max_length=10, choices=KPIUnit.choices, default=KPIUnit.PERCENT)
    baseline = models.DecimalField("خط الأساس", max_digits=14, decimal_places=2, null=True, blank=True)
    target = models.DecimalField("المستهدف", max_digits=14, decimal_places=2)
    direction = models.CharField("اتجاه الأداء", max_length=6, choices=KPIDirection.choices,
                                 default=KPIDirection.HIGHER)
    frequency = models.CharField("دورية القياس", max_length=10, choices=KPIFrequency.choices,
                                 default=KPIFrequency.QUARTERLY)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                              verbose_name="مسؤول القياس")
    data_source = models.CharField("مصدر البيانات", max_length=255, blank=True,
                                   help_text="مثل: تقرير الرضا السنوي، سجل المستفيدين")
    formula = models.TextField("طريقة الاحتساب", blank=True)

    class Meta:
        verbose_name = "مؤشر أداء"
        verbose_name_plural = "مؤشرات الأداء"
        ordering = ["goal", "id"]

    def __str__(self):
        return self.name

    @property
    def sorted_readings(self):
        # يُرتب في بايثون ليستفيد من prefetch_related في اللوحات والتقارير
        return sorted(self.readings.all(), key=lambda r: r.period_end, reverse=True)

    @property
    def latest_reading(self):
        readings = self.sorted_readings
        return readings[0] if readings else None

    @property
    def achievement(self):
        reading = self.latest_reading
        return achievement_percent(reading.value, self.target, self.direction) if reading else None

    @property
    def status(self):
        return achievement_status(self.achievement)

    @property
    def status_label(self):
        return KPIStatus(self.status).label

    @property
    def history(self):
        """القراءات (الأحدث أولاً) مع نسبة تحقق كل قراءة."""
        rows = []
        for reading in self.sorted_readings:
            achievement = achievement_percent(reading.value, self.target, self.direction)
            rows.append({"reading": reading, "achievement": achievement, "status": achievement_status(achievement)})
        return rows

    def is_stale(self, today=None):
        """لم تُسجل قراءة خلال دورة القياس المتوقعة."""
        today = today or timezone.localdate()
        reading = self.latest_reading
        return reading is None or (today - reading.period_end).days > STALE_DAYS[self.frequency]


class KPIReading(models.Model):
    kpi = models.ForeignKey(KPI, models.CASCADE, related_name="readings", verbose_name="المؤشر")
    period_end = models.DateField("نهاية الفترة")
    value = models.DecimalField("القيمة المتحققة", max_digits=14, decimal_places=2)
    note = models.CharField("ملاحظة", max_length=255, blank=True)

    class Meta:
        verbose_name = "قراءة مؤشر"
        verbose_name_plural = "قراءات المؤشر"
        ordering = ["-period_end"]
        constraints = [models.UniqueConstraint(fields=["kpi", "period_end"], name="kpi_reading_per_period",
                                               violation_error_message="توجد قراءة لهذا المؤشر في نفس الفترة.")]

    def __str__(self):
        return f"{self.kpi} — {self.period_end:%Y-%m-%d}"

    @property
    def achievement(self):
        return achievement_percent(self.value, self.kpi.target, self.kpi.direction)
