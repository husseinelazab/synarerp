from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Company


class Plan(models.Model):
    code = models.SlugField("الرمز", unique=True)
    name = models.CharField("اسم الباقة", max_length=100)
    description = models.CharField("الوصف", max_length=255, blank=True)
    price_monthly = models.DecimalField("السعر الشهري", max_digits=12, decimal_places=2, default=0)
    price_yearly = models.DecimalField("السعر السنوي", max_digits=12, decimal_places=2, default=0)
    max_users = models.PositiveIntegerField("الحد الأقصى للمستخدمين", default=3)
    includes_pos = models.BooleanField("تشمل نقاط البيع", default=False)
    trial_days = models.PositiveIntegerField("أيام التجربة المجانية", default=14)
    is_public = models.BooleanField("تظهر في صفحة الأسعار", default=True)
    sort_order = models.PositiveIntegerField("الترتيب", default=0)

    class Meta:
        verbose_name = "باقة"
        verbose_name_plural = "الباقات"
        ordering = ["sort_order", "price_monthly"]

    def __str__(self):
        return self.name


class SubscriptionStatus(models.TextChoices):
    TRIAL = "trial", "فترة تجريبية"
    ACTIVE = "active", "نشط"
    SUSPENDED = "suspended", "موقوف"
    CANCELLED = "cancelled", "ملغى"


class Subscription(models.Model):
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="subscription", verbose_name="الشركة")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, verbose_name="الباقة")
    status = models.CharField("الحالة", max_length=10, choices=SubscriptionStatus.choices, default=SubscriptionStatus.TRIAL)
    period_end = models.DateField("ينتهي في")
    created_at = models.DateTimeField("تاريخ الاشتراك", auto_now_add=True)

    class Meta:
        verbose_name = "اشتراك"
        verbose_name_plural = "الاشتراكات"
        ordering = ["period_end"]

    def __str__(self):
        return f"{self.company} - {self.plan}"

    @property
    def is_valid(self):
        return (self.status in (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE)
                and self.period_end >= timezone.localdate())

    @property
    def days_left(self):
        return (self.period_end - timezone.localdate()).days

    @property
    def state_label(self):
        if self.status in (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE) and not self.is_valid:
            return "منتهٍ"
        return self.get_status_display()


class PaymentMethod(models.TextChoices):
    BANK = "bank", "تحويل بنكي"
    CARD = "card", "بطاقة / بوابة دفع"
    CASH = "cash", "نقداً"
    OTHER = "other", "أخرى"


class SubscriptionPayment(models.Model):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="payments", verbose_name="الاشتراك")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, verbose_name="الباقة")
    months = models.PositiveIntegerField("عدد الأشهر")
    amount = models.DecimalField("المبلغ", max_digits=12, decimal_places=2)
    method = models.CharField("طريقة الدفع", max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.BANK)
    reference = models.CharField("المرجع", max_length=100, blank=True)
    period_start = models.DateField("من")
    period_end = models.DateField("إلى")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, verbose_name="سجّلها")
    created_at = models.DateTimeField("تاريخ التسجيل", auto_now_add=True)

    class Meta:
        verbose_name = "دفعة اشتراك"
        verbose_name_plural = "دفعات الاشتراكات"
        ordering = ["-created_at"]
