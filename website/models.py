"""الموقع الإلكتروني للجمعية: موقع واحد لكل جمعية على نطاقها المستقل (مثل albirr.org.sa) أو للمعاينة على المنصة."""
import re

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TenantModel

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
SLUG_RE = re.compile(r"^[a-z0-9-]{3,40}$")


def normalize_domain(value):
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://", "", value).split("/")[0].split(":")[0]
    return value[4:] if value.startswith("www.") else value


class Website(TenantModel):
    """إعدادات موقع الجمعية: النطاق والنشر والهوية والتواصل وترخيص جمع التبرعات."""

    slug = models.CharField("المعرّف المختصر", max_length=40, unique=True,
                            help_text="أحرف إنجليزية صغيرة وأرقام وشرطة، يُستخدم لرابط المعاينة /site/<المعرّف>/")
    domain = models.CharField("النطاق المستقل", max_length=253, blank=True, null=True, unique=True,
                              help_text="مثل albirr.org.sa (بدون https و www). يُوجَّه النطاق إلى خادم سِينار")
    is_published = models.BooleanField("منشور للعامة", default=False,
                                       help_text="قبل النشر لا يراه إلا مستخدمو الجمعية (معاينة)")
    tagline = models.CharField("الشعار النصي", max_length=200, blank=True, help_text="عبارة قصيرة تحت اسم الجمعية")
    hero_title = models.CharField("عنوان الواجهة", max_length=200, blank=True)
    hero_subtitle = models.CharField("نص الواجهة", max_length=300, blank=True)
    hero_image = models.TextField("صورة الواجهة", blank=True, editable=False)  # data URI مثل الشعار
    about = models.TextField("نبذة عن الجمعية", blank=True)
    primary_color = models.CharField("اللون الرئيسي", max_length=7, default="#1e6e5a")
    whatsapp = models.CharField("واتساب", max_length=20, blank=True)
    x_url = models.URLField("حساب X", blank=True)
    instagram_url = models.URLField("إنستغرام", blank=True)
    snapchat_url = models.URLField("سناب شات", blank=True)
    youtube_url = models.URLField("يوتيوب", blank=True)
    linkedin_url = models.URLField("لينكدإن", blank=True)
    bank_accounts = models.TextField("الحسابات البنكية للتبرع", blank=True, help_text="حساب في كل سطر: البنك - الآيبان")
    map_url = models.URLField("رابط الموقع على الخريطة", blank=True)
    working_hours = models.CharField("أوقات العمل", max_length=150, blank=True)
    fundraising_license_number = models.CharField("رقم ترخيص جمع التبرعات", max_length=50, blank=True,
                                                  help_text="ترخيص المركز الوطني لتنمية القطاع غير الربحي")
    fundraising_license_expiry = models.DateField("انتهاء ترخيص جمع التبرعات", null=True, blank=True)

    class Meta:
        verbose_name = "الموقع الإلكتروني"
        verbose_name_plural = "المواقع الإلكترونية"
        constraints = [models.UniqueConstraint(fields=["company"], name="website_one_per_company")]

    def __str__(self):
        return self.domain or self.slug

    def clean(self):
        self.slug = (self.slug or "").strip().lower()
        if not SLUG_RE.fullmatch(self.slug):
            raise ValidationError({"slug": "المعرّف من 3 إلى 40 حرفاً: أحرف إنجليزية صغيرة وأرقام وشرطة."})
        self.domain = normalize_domain(self.domain) or None
        if self.domain and not DOMAIN_RE.fullmatch(self.domain):
            raise ValidationError({"domain": "اكتب النطاق فقط، مثل albirr.org.sa."})
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", self.primary_color or ""):
            raise ValidationError({"primary_color": "أدخل لوناً بصيغة #RRGGBB."})

    @property
    def fundraising_licensed(self):
        """جمع التبرعات عبر الموقع يتطلب ترخيصاً سارياً من المركز الوطني."""
        return bool(self.fundraising_license_number) and (
            self.fundraising_license_expiry is None or self.fundraising_license_expiry >= timezone.localdate())

    @property
    def bank_account_lines(self):
        return [line.strip() for line in self.bank_accounts.splitlines() if line.strip()]

    @property
    def public_url(self):
        return f"https://{self.domain}/" if self.domain else f"/site/{self.slug}/"

    @classmethod
    def for_company(cls, company):
        site = cls.all_objects.filter(company=company).first()
        if site is None:
            site = cls(company=company, slug=f"org-{company.pk}")
        return site


# مرجع ما يصل من نماذج الموقع: «رقم الجهة / مرجعها» في الوارد، وبداية ملاحظات ملف المستفيد
WEB_SUBMISSION = "الموقع الإلكتروني"
APPLY_NOTE = "طلب مقدم عبر الموقع الإلكتروني"


def current_year():
    return timezone.localdate().year


class NewsCategory(models.TextChoices):
    NEWS = "news", "خبر"
    EVENT = "event", "فعالية"
    ANNOUNCEMENT = "announcement", "إعلان"


class NewsPost(TenantModel):
    """أخبار الجمعية وفعالياتها وإعلاناتها في المركز الإعلامي."""

    title = models.CharField("العنوان", max_length=200)
    category = models.CharField("التصنيف", max_length=12, choices=NewsCategory.choices, default=NewsCategory.NEWS)
    published_on = models.DateField("تاريخ النشر", default=timezone.localdate)
    summary = models.CharField("الملخص", max_length=300, blank=True, help_text="يظهر في بطاقة الخبر ووصف الصفحة")
    body = models.TextField("نص الخبر", blank=True)
    cover = models.TextField("صورة الغلاف", blank=True, editable=False)  # data URI
    is_published = models.BooleanField("منشور", default=True)

    class Meta:
        verbose_name = "خبر"
        verbose_name_plural = "الأخبار"
        ordering = ["-published_on", "-id"]

    def __str__(self):
        return self.title

    @property
    def excerpt(self):
        return self.summary or self.body[:220]


class Album(TenantModel):
    """ألبوم صور لفعالية أو برنامج في معرض الصور."""

    title = models.CharField("عنوان الألبوم", max_length=200)
    event_date = models.DateField("تاريخ الفعالية", null=True, blank=True)
    description = models.TextField("الوصف", blank=True)
    is_published = models.BooleanField("منشور", default=True)

    class Meta:
        verbose_name = "ألبوم الصور"
        verbose_name_plural = "ألبومات الصور"
        ordering = ["-event_date", "-id"]

    def __str__(self):
        return self.title

    @property
    def cover(self):
        first = self.photos.first()
        return first.image if first else ""


class AlbumPhoto(models.Model):
    album = models.ForeignKey(Album, models.CASCADE, related_name="photos", verbose_name="الألبوم")
    image = models.TextField("الصورة")  # data URI
    caption = models.CharField("الوصف", max_length=200, blank=True)
    sort = models.PositiveSmallIntegerField("الترتيب", default=0)

    class Meta:
        verbose_name = "صورة"
        verbose_name_plural = "الصور"
        ordering = ["sort", "id"]

    def __str__(self):
        return self.caption or f"صورة {self.pk}"


class DocumentCategory(models.TextChoices):
    POLICIES = "policies", "اللوائح والسياسات"
    ANNUAL_REPORTS = "annual", "التقارير السنوية"
    FINANCIALS = "financials", "القوائم المالية"
    ASSEMBLY = "assembly", "محاضر الجمعية العمومية"
    STRATEGY = "strategy", "الخطط الاستراتيجية"
    LICENSES = "licenses", "شهادات وتراخيص"
    OTHER = "other", "أخرى"


class PublishedDocument(TenantModel):
    """وثيقة حوكمة وإفصاح منشورة للعامة (PDF أو صورة) تُحفظ في قاعدة البيانات."""

    MAX_BYTES = 10 * 1024 * 1024

    title = models.CharField("عنوان الوثيقة", max_length=200)
    category = models.CharField("التصنيف", max_length=12, choices=DocumentCategory.choices,
                                default=DocumentCategory.POLICIES)
    year = models.PositiveSmallIntegerField("السنة", default=current_year)
    description = models.CharField("الوصف", max_length=300, blank=True)
    file_name = models.CharField("اسم الملف", max_length=255, blank=True, editable=False)
    content_type = models.CharField("نوع الملف", max_length=100, blank=True, editable=False)
    size = models.PositiveIntegerField("الحجم", default=0, editable=False)
    content = models.BinaryField(default=b"", editable=False)
    is_published = models.BooleanField("منشورة", default=True)

    class Meta:
        verbose_name = "وثيقة حوكمة وإفصاح"
        verbose_name_plural = "وثائق الحوكمة والإفصاح"
        ordering = ["category", "-year", "-id"]

    def __str__(self):
        return self.title

    @property
    def has_file(self):
        return bool(self.size)

    @property
    def size_label(self):
        return f"{self.size / 1024 / 1024:.1f} م.ب" if self.size >= 1024 * 1024 else f"{max(self.size // 1024, 1)} ك.ب"


class Partner(TenantModel):
    """شركاء النجاح والداعمون في شريط الشركاء بالموقع."""

    name = models.CharField("اسم الشريك", max_length=150)
    logo = models.TextField("الشعار", blank=True, editable=False)  # data URI
    url = models.URLField("رابط موقع الشريك", blank=True)
    sort = models.PositiveSmallIntegerField("الترتيب", default=0)
    is_published = models.BooleanField("يظهر في الموقع", default=True)

    class Meta:
        verbose_name = "شريك نجاح"
        verbose_name_plural = "شركاء النجاح"
        ordering = ["sort", "id"]

    def __str__(self):
        return self.name
