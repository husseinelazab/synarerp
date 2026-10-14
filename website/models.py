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
