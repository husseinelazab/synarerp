"""حقول التسعير المشتركة لمستندات البيع والشراء وأسطرها."""
from django.db import models


class PricedDocument(models.Model):
    price_list = models.ForeignKey("pricing.PriceList", models.PROTECT, null=True, blank=True, related_name="+",
                                   verbose_name="قائمة الأسعار",
                                   help_text="فارغة = قائمة الطرف أو فئته أو القائمة الافتراضية")
    coupon_code = models.CharField("كود الكوبون", max_length=40, blank=True)
    ignore_pricing_rules = models.BooleanField("تجاهل العروض وقواعد التسعير", default=False)

    class Meta:
        abstract = True


class PricedLine(models.Model):
    price_list_rate = models.DecimalField("سعر القائمة", max_digits=18, decimal_places=2, default=0, editable=False)
    discount_percentage = models.DecimalField("الخصم %", max_digits=6, decimal_places=2, default=0, editable=False)
    pricing_rule = models.ForeignKey("pricing.PricingRule", models.SET_NULL, null=True, blank=True, related_name="+",
                                     editable=False, verbose_name="العرض المطبق")
    is_free_item = models.BooleanField("هدية", default=False, editable=False)

    class Meta:
        abstract = True
