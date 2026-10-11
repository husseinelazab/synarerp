"""رسائل SMS وواتساب للعملاء.

الإرسال عبر مزوّد معطّل افتراضياً: لا يتصل النظام بأي مزوّد حتى يُدخل المدير بياناته ويفعّله بنفسه
(زر «رسالة اختبار» هو الاستثناء الوحيد قبل التفعيل). بدون مزوّد يمكن دائماً فتح واتساب على جهاز المستخدم
برابط wa.me جاهز بنص الرسالة — دون أي اتصال من الخادم.
"""
from django.db import models

from core.models import Company, TenantModel


class Provider(models.TextChoices):
    WHATSAPP_CLOUD = "whatsapp", "واتساب للأعمال (WhatsApp Cloud API من Meta)"
    UNIFONIC = "unifonic", "SMS — يونيفونك (Unifonic)"
    TAQNYAT = "taqnyat", "SMS — تقنيات (Taqnyat)"
    MSEGAT = "msegat", "SMS — مسجات (Msegat)"
    HTTP = "http", "مزوّد آخر عبر رابط HTTP (JSON)"


class Event(models.TextChoices):
    INVOICE = "invoice", "فاتورة مبيعات جديدة"
    PAYMENT = "payment", "استلام دفعة من العميل"
    OVERDUE = "overdue", "تذكير بفاتورة متأخرة"
    STATEMENT = "statement", "رصيد العميل (كشف مختصر)"


DEFAULT_TEMPLATES = {
    Event.INVOICE: "عميلنا العزيز {customer}، صدرت فاتورتكم رقم {invoice} بمبلغ {amount}{due_text}.{link_text} "
                   "شكراً لتعاملكم مع {company}.",
    Event.PAYMENT: "عميلنا العزيز {customer}، استلمنا دفعتكم بمبلغ {amount}. رصيدكم الحالي {balance}. شكراً لكم — {company}.",
    Event.OVERDUE: "عميلنا العزيز {customer}، نذكّركم بأن الفاتورة {invoice} بمبلغ متبقٍ {amount} متأخرة {days} يوماً عن موعد "
                   "استحقاقها {due_date}. نرجو السداد{link_text} — {company}.",
    Event.STATEMENT: "عميلنا العزيز {customer}، رصيدكم المستحق لدى {company} هو {balance}{overdue_text}. شكراً لكم.",
}
PLACEHOLDERS = "{customer} {invoice} {amount} {due_date} {days} {balance} {company} {link}"


class MessagingSettings(TenantModel):
    enabled = models.BooleanField("تفعيل الإرسال عبر المزوّد", default=False,
                                  help_text="بدون تفعيل: تُفتح الرسالة في واتساب على جهازك فقط، ولا يتصل النظام بأي جهة")
    provider = models.CharField("المزوّد", max_length=10, choices=Provider.choices, default=Provider.WHATSAPP_CLOUD)
    sender = models.CharField("اسم المرسل / معرّف رقم واتساب", max_length=100, blank=True,
                              help_text="SMS: اسم المرسل المعتمد لدى هيئة الاتصالات. واتساب: Phone number ID من Meta")
    account = models.CharField("اسم المستخدم / AppSid", max_length=150, blank=True,
                               help_text="يونيفونك: AppSid. مسجات: اسم المستخدم. غير مطلوب لواتساب وتقنيات")
    token_encrypted = models.TextField(blank=True, editable=False)
    api_url = models.URLField("رابط الإرسال", blank=True,
                              help_text="للمزوّد الآخر فقط (أو لتغيير رابط مزوّد معروف)")
    http_body = models.TextField("قالب جسم الطلب (JSON)", blank=True,
                                 default='{"to": "{to}", "message": "{message}", "sender": "{sender}"}',
                                 help_text="للمزوّد الآخر: {to} الرقم و{message} النص و{sender} المرسل")
    whatsapp_template = models.CharField("اسم قالب واتساب المعتمد", max_length=100, blank=True,
                                         help_text="واتساب يشترط قالباً معتمداً لبدء المحادثة؛ يُمرَّر نص الرسالة كمتغير {{1}}. "
                                                   "اتركه فارغاً لإرسال نص حر (يصل فقط إن راسلك العميل خلال 24 ساعة)")
    whatsapp_language = models.CharField("لغة القالب", max_length=10, default="ar", blank=True)
    country_code = models.CharField("رمز الدولة الافتراضي", max_length=4, default="966")
    auto_invoice = models.BooleanField("رسالة تلقائية عند اعتماد فاتورة آجلة", default=False)
    auto_payment = models.BooleanField("رسالة تلقائية عند استلام دفعة", default=False)
    auto_overdue = models.BooleanField("تذكير تلقائي بالفواتير المتأخرة", default=False)
    overdue_every_days = models.PositiveSmallIntegerField("تكرار التذكير كل (يوم)", default=7)
    last_test_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_test_ok = models.BooleanField(default=False, editable=False)
    last_test_message = models.CharField(max_length=500, blank=True, editable=False)

    class Meta:
        verbose_name = "إعدادات الرسائل"
        verbose_name_plural = "إعدادات الرسائل"
        constraints = [models.UniqueConstraint(fields=["company"], name="one_messaging_settings_per_company")]

    @classmethod
    def for_company(cls, company):
        obj = cls.all_objects.filter(company=company).first()
        return obj or cls.all_objects.create(company=company)

    @property
    def has_token(self):
        return bool(self.token_encrypted)

    @property
    def token(self):
        from core.mail import decrypt_password

        return decrypt_password(self.token_encrypted)

    @property
    def is_configured(self):
        if self.provider == Provider.HTTP:
            return bool(self.api_url)
        if self.provider == Provider.UNIFONIC:
            return bool(self.account and self.sender)
        if self.provider == Provider.MSEGAT:
            return bool(self.account and self.sender and self.has_token)
        return bool(self.sender and self.has_token)

    @property
    def active(self):
        return self.enabled and self.is_configured

    @property
    def channel(self):
        return "whatsapp" if self.provider == Provider.WHATSAPP_CLOUD else "sms"


class MessageTemplate(TenantModel):
    event = models.CharField("المناسبة", max_length=12, choices=Event.choices)
    body = models.TextField("نص الرسالة")

    class Meta:
        verbose_name = "قالب رسالة"
        verbose_name_plural = "قوالب الرسائل"
        constraints = [models.UniqueConstraint(fields=["company", "event"], name="one_template_per_event")]

    @classmethod
    def text_for(cls, company, event):
        row = cls.all_objects.filter(company=company, event=event).first()
        return row.body if row else DEFAULT_TEMPLATES[event]


class MessageLog(TenantModel):
    STATUS = [("queued", "قيد الإرسال"), ("sent", "أُرسلت"), ("failed", "فشلت"), ("link", "فُتحت في واتساب")]

    event = models.CharField("المناسبة", max_length=12, choices=Event.choices, blank=True)
    channel = models.CharField("القناة", max_length=10, default="link")
    phone = models.CharField("الرقم", max_length=20)
    body = models.TextField("النص")
    status = models.CharField("الحالة", max_length=10, choices=STATUS, default="queued")
    error = models.CharField("الخطأ", max_length=500, blank=True)
    reference = models.CharField("المرجع", max_length=60, blank=True, db_index=True)
    customer = models.ForeignKey("selling.Customer", models.SET_NULL, null=True, blank=True, related_name="messages",
                                 verbose_name="العميل")
    user = models.ForeignKey("auth.User", models.SET_NULL, null=True, blank=True, related_name="+",
                             verbose_name="المستخدم")
    created_at = models.DateTimeField("الوقت", auto_now_add=True)

    class Meta:
        verbose_name = "رسالة"
        verbose_name_plural = "سجل الرسائل"
        ordering = ["-id"]


__all__ = ["Company", "MessagingSettings", "MessageTemplate", "MessageLog", "Provider", "Event"]
