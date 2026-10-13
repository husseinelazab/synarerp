"""إدارة التوثيق والمستندات (الصادر والوارد ومسار المعاملة والأرشيف) والمكتب الإلكتروني (الطلبات الإدارية والتعاميم)."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import NamingSeries, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique


# ───────────────────────────── الصادر والوارد ─────────────────────────────

class Direction(models.TextChoices):
    INCOMING = "incoming", "وارد"
    OUTGOING = "outgoing", "صادر"
    INTERNAL = "internal", "داخلي"


NUMBER_PREFIX = {Direction.INCOMING: "IN", Direction.OUTGOING: "OUT", Direction.INTERNAL: "INT"}


class Priority(models.TextChoices):
    NORMAL = "normal", "عادي"
    URGENT = "urgent", "عاجل"
    VERY_URGENT = "very_urgent", "عاجل جداً"


class CorrespondenceStatus(models.TextChoices):
    OPEN = "open", "جديدة"
    IN_PROGRESS = "in_progress", "قيد الإجراء"
    CLOSED = "closed", "منتهية (محفوظة)"


class Correspondence(TenantModel):
    """معاملة صادرة أو واردة أو داخلية برقم قيد تلقائي لكل نوع وسنة، ومسار إحالة موثق."""

    direction = models.CharField("النوع", max_length=10, choices=Direction.choices, default=Direction.INCOMING)
    number = models.CharField("رقم القيد", max_length=30, editable=False)
    date = models.DateField("التاريخ", default=timezone.localdate)
    subject = models.CharField("الموضوع", max_length=255)
    party = models.CharField("الجهة", max_length=200, blank=True,
                             help_text="المرسِل للوارد، والمرسَل إليه للصادر")
    entity = models.ForeignKey("beneficiaries.BeneficiaryEntity", models.SET_NULL, null=True, blank=True,
                               related_name="+", verbose_name="الجهة المسجلة")
    external_reference = models.CharField("رقم الجهة / مرجعها", max_length=60, blank=True)
    external_date = models.DateField("تاريخ خطاب الجهة", null=True, blank=True)
    priority = models.CharField("الأولوية", max_length=12, choices=Priority.choices, default=Priority.NORMAL)
    is_confidential = models.BooleanField("سرية", default=False,
                                          help_text="لا تظهر إلا للمدير ومنشئها ومن أُحيلت إليه")
    body = models.TextField("نص الخطاب / الملخص", blank=True)
    reply_to = models.ForeignKey("self", models.SET_NULL, null=True, blank=True, related_name="replies",
                                 verbose_name="رداً على")
    signatory = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                  verbose_name="الموقّع (للصادر)")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                    related_name="+", editable=False, verbose_name="لدى")
    due_date = models.DateField("موعد الإنجاز", null=True, blank=True)
    status = models.CharField("الحالة", max_length=12, choices=CorrespondenceStatus.choices,
                              default=CorrespondenceStatus.OPEN, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                   related_name="+", editable=False, verbose_name="سجّلها")

    class Meta:
        verbose_name = "معاملة"
        verbose_name_plural = "الصادر والوارد"
        ordering = ["-date", "-id"]
        constraints = [tenant_unique("number", model_label="correspondence")]

    def __str__(self):
        return f"{self.number} — {self.subject}" if self.number else self.subject

    def save(self, *args, **kwargs):
        if not self.number:
            from core.models import Company

            company = self.company if self.company_id else Company.current()
            self.number = NamingSeries.next_name(company, f"{NUMBER_PREFIX[self.direction]}-{self.date.year}")
        if self.created_by_id is None:
            from core.tenancy import get_current_user

            self.created_by = get_current_user()
        super().save(*args, **kwargs)

    @classmethod
    def scope_for_user(cls, qs, request):
        membership = getattr(request, "membership", None)
        if membership is not None and membership.can_manage:
            return qs
        user = request.user
        return qs.filter(Q(is_confidential=False) | Q(created_by=user) | Q(assigned_to=user) |
                         Q(routes__to_user=user)).distinct()

    @property
    def route_candidates(self):
        """مستخدمو الجهة الذين يمكن إحالة المعاملة إليهم."""
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(memberships__company_id=self.company_id, memberships__is_active=True,
                                               memberships__role__in=["owner", "admin", "user"]).distinct()

    @property
    def is_overdue(self):
        return self.status != CorrespondenceStatus.CLOSED and self.due_date and self.due_date < timezone.localdate()

    @property
    def available_actions(self):
        if self.status != CorrespondenceStatus.CLOSED:
            return [("close", "إنهاء المعاملة وحفظها")]
        return [("reopen", "إعادة فتح المعاملة")]

    def route(self, user, to_user, note="", due_date=None):
        if self.status == CorrespondenceStatus.CLOSED:
            raise ValidationError("المعاملة منتهية؛ أعد فتحها أولاً.")
        if to_user is None:
            raise ValidationError("حدد الموظف المحال إليه.")
        CorrespondenceRoute.objects.create(correspondence=self, from_user=user, to_user=to_user, note=note[:500])
        self.assigned_to, self.status = to_user, CorrespondenceStatus.IN_PROGRESS
        if due_date:
            self.due_date = due_date
        self.save(update_fields=["assigned_to", "status", "due_date"])
        from core.notify import notify

        notify(self.company, [to_user], f"أُحيلت إليك المعاملة {self} من {user.get_full_name() or user}",
               self.get_absolute_url())

    def close(self, user):
        CorrespondenceRoute.objects.create(correspondence=self, from_user=user, note="إنهاء المعاملة وحفظها")
        self.status = CorrespondenceStatus.CLOSED
        self.save(update_fields=["status"])

    def reopen(self, user):
        self.status = CorrespondenceStatus.IN_PROGRESS
        self.save(update_fields=["status"])


class CorrespondenceRoute(models.Model):
    """سطر في مسار المعاملة: إحالة أو إجراء، بمن وإلى من ومتى."""

    correspondence = models.ForeignKey(Correspondence, models.CASCADE, related_name="routes")
    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, related_name="+")
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


# ───────────────────────────── الأرشيف ─────────────────────────────

class ArchiveCategory(TenantModel):
    name = models.CharField("التصنيف", max_length=100)
    parent = models.ForeignKey("self", models.PROTECT, null=True, blank=True, related_name="children",
                               verbose_name="التصنيف الأب")

    class Meta:
        verbose_name = "تصنيف أرشيف"
        verbose_name_plural = "تصنيفات الأرشيف"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="archivecategory")]

    def __str__(self):
        return f"{self.parent} / {self.name}" if self.parent_id else self.name


class ArchiveDocument(TenantModel):
    """وثيقة مؤرشفة (عقد، صك، ترخيص، محضر قديم…) تُرفق ملفاتها وتُبحث بالعنوان والكلمات المفتاحية."""

    title = models.CharField("عنوان الوثيقة", max_length=255)
    category = models.ForeignKey(ArchiveCategory, models.PROTECT, related_name="documents", verbose_name="التصنيف")
    document_date = models.DateField("تاريخ الوثيقة", null=True, blank=True)
    reference = models.CharField("الرقم المرجعي", max_length=60, blank=True)
    keywords = models.CharField("كلمات مفتاحية", max_length=255, blank=True)
    expiry_date = models.DateField("تاريخ الانتهاء", null=True, blank=True,
                                   help_text="للتراخيص والعقود: يظهر تنبيه قبل الانتهاء بشهر")
    physical_location = models.CharField("مكان الأصل الورقي", max_length=150, blank=True,
                                         help_text="مثل: الدولاب 2 — الملف 14")
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "وثيقة مؤرشفة"
        verbose_name_plural = "الأرشيف"
        ordering = ["-document_date", "-id"]

    def __str__(self):
        return self.title

    @property
    def expires_soon(self):
        return bool(self.expiry_date and self.expiry_date <= timezone.localdate() + timezone.timedelta(days=30))


# ───────────────────────────── المكتب الإلكتروني ─────────────────────────────

class AdminRequestType(TenantModel):
    name = models.CharField("نوع الطلب", max_length=100)
    description = models.CharField("الوصف / المتطلبات", max_length=255, blank=True)

    class Meta:
        verbose_name = "نوع طلب إداري"
        verbose_name_plural = "أنواع الطلبات الإدارية"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="adminrequesttype")]

    def __str__(self):
        return self.name


class AdminRequest(SubmittableDocument):
    """طلب إداري من موظف: يمر بسلاسل الاعتماد (الإعدادات ← سلاسل الاعتماد) ثم يُنفّذ."""

    naming_prefix = "REQ"
    posts_to_ledger = False
    posting_note = "الطلب معتمد؛ نفّذه ثم أغلقه من «تم التنفيذ»."

    request_type = models.ForeignKey(AdminRequestType, models.PROTECT, related_name="+", verbose_name="نوع الطلب")
    subject = models.CharField("الموضوع", max_length=200)
    details = models.TextField("التفاصيل", blank=True)
    amount = models.DecimalField("المبلغ (إن وجد)", max_digits=14, decimal_places=2, default=0,
                                 help_text="مثل طلب شراء أو سلفة عهدة؛ تستخدمه سلاسل الاعتماد حسب القيمة")
    needed_by = models.DateField("مطلوب قبل", null=True, blank=True)
    done = models.BooleanField("تم التنفيذ", default=False, editable=False)
    done_note = models.CharField("ملاحظة التنفيذ", max_length=255, blank=True, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "طلب إداري"
        verbose_name_plural = "الطلبات الإدارية"

    def __str__(self):
        return f"{self.name} — {self.subject}" if self.name else self.subject

    @property
    def available_actions(self):
        return [("mark_done", "تم التنفيذ")] if self.is_submitted and not self.done else []

    def mark_done(self, user):
        self.done, self.done_note = True, f"نُفذ بواسطة {user.get_full_name() or user} {timezone.localdate():%Y-%m-%d}"
        self.save(update_fields=["done", "done_note"])
        from core.notify import notify

        if self.created_by_id:
            notify(self.company, [self.created_by], f"تم تنفيذ طلبك {self}", self.get_absolute_url())

    def on_submit(self):
        from core.notify import notify

        if self.created_by_id:
            notify(self.company, [self.created_by], f"اعتُمد طلبك {self}", self.get_absolute_url())


class Circular(TenantModel):
    """تعميم داخلي لكل المستخدمين، مع تأكيد الاطلاع لمن يُطلب منه."""

    title = models.CharField("عنوان التعميم", max_length=200)
    body = models.TextField("نص التعميم")
    date = models.DateField("التاريخ", default=timezone.localdate)
    requires_ack = models.BooleanField("يتطلب تأكيد الاطلاع", default=True)
    expires_on = models.DateField("يظهر حتى", null=True, blank=True)

    class Meta:
        verbose_name = "تعميم"
        verbose_name_plural = "التعاميم"
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.title

    def acknowledge(self, user):
        CircularAck.objects.get_or_create(circular=self, user=user)

    @property
    def ack_count(self):
        return self.acks.count()


class CircularAck(models.Model):
    circular = models.ForeignKey(Circular, models.CASCADE, related_name="acks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, related_name="+")
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["circular", "user"], name="one_ack_per_user")]
