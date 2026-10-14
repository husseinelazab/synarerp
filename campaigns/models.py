"""حملات التواصل مع الداعمين: شرائح تُحسب حيّة، وحملات SMS/بريد، وقائمة إيقاف، وتذكيرات الكفالات الآلية."""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TenantModel
from core.tenancy import tenant_unique


class SegmentRule(models.TextChoices):
    ALL_DONORS = "all_donors", "جميع الداعمين (لهم جوال أو بريد)"
    LAPSED = "lapsed", "داعمون منقطعون (تبرعوا سابقاً ولم يتبرعوا منذ N شهر)"
    MAJOR = "major", "كبار الداعمين (إجمالي آخر 12 شهراً ≥ مبلغ)"
    ACTIVE_SPONSORS = "sponsors", "الكافلون النشطون (كفالات سارية)"
    OVERDUE_SPONSORS = "overdue", "كافلون عليهم استحقاقات متأخرة"
    RECURRING = "recurring", "المتبرعون الشهريون عبر المتجر"
    STORE_DONORS = "store", "متبرعو المتجر الإلكتروني (طلبات مدفوعة)"
    NEWSLETTER = "newsletter", "مشتركو النشرة البريدية (بريد فقط)"
    FUND_DONORS = "fund", "متبرعو صندوق / نوع فرصة محدد"


class Segment(TenantModel):
    """شريحة: قاعدة تُقيَّم عند الطلب إلى قائمة مستلمين {الاسم، الجوال، البريد، المصدر}."""

    name = models.CharField("اسم الشريحة", max_length=150)
    rule = models.CharField("القاعدة", max_length=12, choices=SegmentRule.choices, default=SegmentRule.ALL_DONORS)
    months = models.PositiveSmallIntegerField("عدد الأشهر", default=6,
                                             help_text="للمنقطعين: لم يتبرعوا خلال هذه المدة")
    min_amount = models.DecimalField("الحد الأدنى للمبلغ", max_digits=14, decimal_places=2, default=5000,
                                     help_text="لكبار الداعمين: إجمالي تبرعاتهم في آخر 12 شهراً")
    fund = models.ForeignKey("accounts.Fund", models.SET_NULL, null=True, blank=True, related_name="+",
                             verbose_name="الصندوق", help_text="لقاعدة متبرعي صندوق: سندات التبرع في هذا الصندوق")
    opportunity_kind = models.CharField("نوع فرصة المتجر", max_length=12, blank=True,
                                        choices=[("", "— الكل —")] + [
                                            ("general", "عام"), ("zakat", "زكاة"), ("project", "مشروع"),
                                            ("case", "حالة"), ("sponsorship", "كفالة"), ("waqf", "سهم وقفي"),
                                            ("restricted", "مقيد بغرض")],
                                        help_text="لقاعدة متبرعي صندوق: طلبات المتجر المدفوعة في فرص من هذا النوع")
    description = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "شريحة"
        verbose_name_plural = "الشرائح"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="campaign_segment")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.rule == SegmentRule.FUND_DONORS and not self.fund_id and not self.opportunity_kind:
            raise ValidationError("حدد الصندوق أو نوع فرصة المتجر لقاعدة «متبرعي صندوق».")
        if self.rule == SegmentRule.LAPSED and not self.months:
            raise ValidationError({"months": "حدد عدد الأشهر."})

    def recipients(self):
        from campaigns.segments import evaluate

        if not hasattr(self, "_recipients"):
            self._recipients = evaluate(self)
        return self._recipients

    @property
    def preview(self):
        rows = self.recipients()
        return {"count": len(rows), "rows": rows[:20], "phones": sum(1 for r in rows if r["phone"]),
                "emails": sum(1 for r in rows if r["email"])}


class Channel(models.TextChoices):
    SMS = "sms", "رسالة SMS"
    EMAIL = "email", "بريد إلكتروني"
    BOTH = "both", "SMS وبريد إلكتروني"


class CampaignStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    SCHEDULED = "scheduled", "مجدولة"
    SENDING = "sending", "قيد الإرسال"
    SENT = "sent", "أُرسلت"
    CANCELLED = "cancelled", "ملغاة"


PLACEHOLDER_HELP = "المتغيرات: {الاسم} اسم المستلم، {الجمعية} اسم الجمعية، {الرابط} الرابط أدناه"


class Campaign(TenantModel):
    title = models.CharField("عنوان الحملة", max_length=150)
    channel = models.CharField("القناة", max_length=6, choices=Channel.choices, default=Channel.SMS)
    segment = models.ForeignKey(Segment, models.PROTECT, related_name="campaigns", verbose_name="الشريحة")
    email_subject = models.CharField("عنوان البريد", max_length=150, blank=True,
                                     help_text="للبريد الإلكتروني؛ فارغ = عنوان الحملة")
    message = models.TextField("نص الرسالة", help_text=PLACEHOLDER_HELP)
    link = models.URLField("الرابط", blank=True, help_text="يحل محل {الرابط}، مثل رابط فرصة في المتجر")
    opportunity = models.ForeignKey("store.DonationOpportunity", models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="فرصة المتجر",
                                    help_text="اختياري: إن لم يُحدد رابط يُستخدم رابط هذه الفرصة في موقع الجمعية")
    scheduled_at = models.DateTimeField("موعد الإرسال", null=True, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=CampaignStatus.choices, default=CampaignStatus.DRAFT,
                              editable=False)
    sent_at = models.DateTimeField("تاريخ الإرسال", null=True, blank=True, editable=False)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)

    class Meta:
        verbose_name = "حملة"
        verbose_name_plural = "الحملات"
        ordering = ["-id"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.pk and self.status in (CampaignStatus.SENDING, CampaignStatus.SENT, CampaignStatus.CANCELLED):
            raise ValidationError("لا يمكن تعديل حملة أُرسلت أو أُلغيت.")
        if "{الرابط}" in (self.message or "") and not self.link and not self.opportunity_id:
            raise ValidationError({"link": "النص يحتوي {الرابط}: أدخل الرابط أو اختر فرصة من المتجر."})

    # ---------- الإحصاءات ----------
    @property
    def stats(self):
        if self.status == CampaignStatus.SENT:
            from campaigns.service import sync_statuses

            sync_statuses(self)
        counts = dict(self.recipients.values_list("status").annotate(n=models.Count("id")))
        total = sum(counts.values())
        return {"total": total, "queued": counts.get("queued", 0), "sent": counts.get("sent", 0),
                "failed": counts.get("failed", 0), "skipped": counts.get("skipped", 0)}

    @property
    def sms_info(self):
        from campaigns.service import sms_segments

        return sms_segments(self.message or "")

    # ---------- الإجراءات ----------
    @property
    def available_actions(self):
        if self.status in (CampaignStatus.DRAFT, CampaignStatus.SCHEDULED):
            actions = [("send_now", "إرسال الآن")]
            if self.status == CampaignStatus.DRAFT:
                actions.append(("schedule", "جدولة"))
            return actions + [("cancel_campaign", "إلغاء")]
        if self.status == CampaignStatus.SENDING:
            return [("send_now", "استكمال الإرسال")]
        if self.status == CampaignStatus.SENT and self.recipients.filter(status="failed").exists():
            return [("retry_failed", "إعادة محاولة الفاشلة")]
        return []

    def send_now(self, user):
        from campaigns.service import send_campaign

        if self.status not in (CampaignStatus.DRAFT, CampaignStatus.SCHEDULED, CampaignStatus.SENDING):
            raise ValidationError("لا يمكن إرسال هذه الحملة في حالتها الحالية.")
        if self.status != CampaignStatus.SENDING and not self.segment.recipients():
            raise ValidationError("الشريحة لا تحتوي أي مستلم لديه جوال أو بريد.")
        send_campaign(self)

    def schedule(self, user):
        if self.status != CampaignStatus.DRAFT:
            raise ValidationError("الجدولة للمسودات فقط.")
        if not self.scheduled_at:
            raise ValidationError("حدد «موعد الإرسال» في الحملة أولاً ثم اضغط جدولة.")
        if self.scheduled_at <= timezone.now():
            raise ValidationError("موعد الإرسال مضى؛ اختر موعداً قادماً أو أرسل الآن.")
        self.status = CampaignStatus.SCHEDULED
        self.save(update_fields=["status"])

    def cancel_campaign(self, user):
        if self.status not in (CampaignStatus.DRAFT, CampaignStatus.SCHEDULED):
            raise ValidationError("لا يمكن إلغاء حملة بدأ إرسالها.")
        self.status = CampaignStatus.CANCELLED
        self.save(update_fields=["status"])

    def retry_failed(self, user):
        from campaigns.service import send_campaign

        self.recipients.filter(status="failed").update(status="queued", error="", attempted_at=None)
        send_campaign(self)


class CampaignRecipient(TenantModel):
    STATUS = [("queued", "في الانتظار"), ("sent", "أُرسلت"), ("failed", "فشلت"), ("skipped", "مستبعد")]

    campaign = models.ForeignKey(Campaign, models.CASCADE, related_name="recipients", verbose_name="الحملة")
    channel = models.CharField("القناة", max_length=6)
    address = models.CharField("الجوال / البريد", max_length=254)
    name = models.CharField("الاسم", max_length=150, blank=True)
    source = models.CharField("المصدر", max_length=60, blank=True)
    status = models.CharField("الحالة", max_length=8, choices=STATUS, default="queued")
    error = models.CharField("السبب", max_length=500, blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    message_log = models.ForeignKey("messaging.MessageLog", models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "مستلم حملة"
        verbose_name_plural = "مستلمو الحملات"
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["campaign", "channel", "address"],
                                               name="campaign_recipient_once")]

    def __str__(self):
        return f"{self.name or self.address} ({self.channel})"


class OptOut(TenantModel):
    phone = models.CharField("الجوال", max_length=20, blank=True, db_index=True)
    email = models.EmailField("البريد الإلكتروني", blank=True, db_index=True)
    reason = models.CharField("السبب", max_length=200, blank=True)
    source = models.CharField("المصدر", max_length=20, blank=True, editable=False, default="manual")
    created_at = models.DateTimeField("التاريخ", auto_now_add=True)

    class Meta:
        verbose_name = "طلب إيقاف"
        verbose_name_plural = "قائمة الإيقاف"
        ordering = ["-id"]

    def __str__(self):
        return self.phone or self.email

    def clean(self):
        if not self.phone and not self.email:
            raise ValidationError("أدخل الجوال أو البريد.")

    def save(self, *args, **kwargs):
        from campaigns.service import norm_email, norm_phone

        company = self.company if self.company_id else None
        self.phone = norm_phone(self.phone, company) if self.phone else ""
        self.email = norm_email(self.email)
        super().save(*args, **kwargs)


DEFAULT_DUE_TEXT = ("عزيزنا {الاسم}، نذكّركم بموعد كفالتكم لشهر {الشهر} بمبلغ {المبلغ} ريال. "
                    "جزاكم الله خيراً — {الجمعية}")
DEFAULT_OVERDUE_TEXT = ("عزيزنا {الاسم}، لديكم استحقاقات كفالة متأخرة بمبلغ {المبلغ} ريال. "
                        "نشكر عطاءكم المستمر — {الجمعية}")


class CampaignSettings(TenantModel):
    sms_optout_footer = models.BooleanField("إضافة «للإيقاف أرسل: إيقاف» لرسائل الحملات", default=True)
    due_reminder_enabled = models.BooleanField("تذكير الكافلين قبل موعد الكفالة الشهرية", default=False)
    due_days_before = models.PositiveSmallIntegerField("قبل الموعد بـ (يوم)", default=3,
                                                       help_text="الموعد هو أول الشهر")
    due_text = models.TextField("نص تذكير الموعد", default=DEFAULT_DUE_TEXT,
                                help_text="المتغيرات: {الاسم} {الجمعية} {المبلغ} {الشهر}")
    overdue_reminder_enabled = models.BooleanField("تذكير شهري بالاستحقاقات المتأخرة", default=False)
    overdue_text = models.TextField("نص تذكير المتأخرات", default=DEFAULT_OVERDUE_TEXT,
                                    help_text="المتغيرات: {الاسم} {الجمعية} {المبلغ}")

    class Meta:
        verbose_name = "إعدادات التذكير"
        verbose_name_plural = "إعدادات التذكير"
        constraints = [models.UniqueConstraint(fields=["company"], name="one_campaign_settings_per_company")]

    @classmethod
    def for_company(cls, company):
        obj = cls.all_objects.filter(company=company).first()
        return obj or cls.all_objects.create(company=company)


class ReminderLog(TenantModel):
    """يمنع تكرار التذكير الآلي: مفتاح واحد لكل داعم ونوع وفترة."""

    key = models.CharField(max_length=80)
    phone = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=8, default="sent")
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "key"], name="campaign_reminder_once")]
