"""قوائم الأسعار وقواعد التسعير (العروض والخصومات والكوبونات) وبرنامج الولاء."""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TenantModel
from core.tenancy import tenant_unique


class PriceKind(models.TextChoices):
    SELLING = "selling", "بيع"
    BUYING = "buying", "شراء"


class PriceList(TenantModel):
    """قائمة أسعار (جملة، تجزئة، كبار العملاء، أسعار مورد...)."""

    name = models.CharField("اسم القائمة", max_length=100)
    kind = models.CharField("النوع", max_length=10, choices=PriceKind.choices, default=PriceKind.SELLING)
    currency = models.ForeignKey("core.Currency", models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="العملة", help_text="فارغة = عملة الشركة")
    is_default = models.BooleanField("القائمة الافتراضية لهذا النوع", default=False,
                                     help_text="تُستخدم عندما لا يكون للعميل/المورد أو مجموعته قائمة خاصة")
    disabled = models.BooleanField("موقوفة", default=False)

    class Meta:
        verbose_name = "قائمة أسعار"
        verbose_name_plural = "قوائم الأسعار"
        ordering = ["kind", "name"]
        constraints = [tenant_unique("name", model_label="pricelist")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            PriceList.all_objects.filter(company=self.company, kind=self.kind, is_default=True).exclude(
                pk=self.pk).update(is_default=False)


class ItemPrice(TenantModel):
    price_list = models.ForeignKey(PriceList, models.CASCADE, related_name="prices", verbose_name="قائمة الأسعار")
    item = models.ForeignKey("stock.Item", models.CASCADE, related_name="list_prices", verbose_name="الصنف")
    uom = models.ForeignKey("stock.UOM", models.PROTECT, null=True, blank=True, related_name="+",
                            verbose_name="الوحدة", help_text="فارغة = الوحدة الأساسية؛ وحدات التحويل تُحسب بالمعامل إن لم يُحدد لها سعر")
    rate = models.DecimalField("السعر", max_digits=18, decimal_places=2)
    valid_from = models.DateField("ساري من", null=True, blank=True)
    valid_upto = models.DateField("ساري حتى", null=True, blank=True)

    class Meta:
        verbose_name = "سعر صنف"
        verbose_name_plural = "أسعار الأصناف في القوائم"
        ordering = ["price_list", "item"]

    def __str__(self):
        return f"{self.item} — {self.price_list}: {self.rate}"

    def clean(self):
        if self.rate is not None and self.rate < 0:
            raise ValidationError({"rate": "السعر لا يكون سالباً."})
        if self.valid_from and self.valid_upto and self.valid_from > self.valid_upto:
            raise ValidationError("تاريخ البداية بعد تاريخ النهاية.")


class ApplyOn(models.TextChoices):
    ITEM = "item", "صنف محدد"
    ITEM_GROUP = "item_group", "مجموعة أصناف"
    ALL = "all", "كل الأصناف"


class DiscountType(models.TextChoices):
    PERCENTAGE = "percentage", "نسبة خصم %"
    AMOUNT = "amount", "خصم مبلغ ثابت من سعر الوحدة"
    RATE = "rate", "سعر خاص للوحدة"
    NONE = "none", "بلا خصم سعر (هدية فقط)"


class Channel(models.TextChoices):
    ALL = "all", "كل المبيعات"
    POS = "pos", "نقاط البيع فقط"
    BACKOFFICE = "backoffice", "الفواتير والأوامر فقط"


class PricingRule(TenantModel):
    """قاعدة تسعير: خصم أو سعر خاص أو هدية (اشترِ X واحصل على Y) بشروط الطرف والكمية والتاريخ والكوبون."""

    name = models.CharField("اسم العرض / القاعدة", max_length=150)
    kind = models.CharField("تطبق على", max_length=10, choices=PriceKind.choices, default=PriceKind.SELLING)
    apply_on = models.CharField("نطاق الأصناف", max_length=12, choices=ApplyOn.choices, default=ApplyOn.ITEM)
    item = models.ForeignKey("stock.Item", models.CASCADE, null=True, blank=True, related_name="+",
                             verbose_name="الصنف")
    item_group = models.ForeignKey("stock.ItemGroup", models.CASCADE, null=True, blank=True, related_name="+",
                                   verbose_name="مجموعة الأصناف", help_text="تشمل المجموعات الفرعية")
    customer = models.ForeignKey("selling.Customer", models.CASCADE, null=True, blank=True, related_name="+",
                                 verbose_name="لعميل محدد")
    customer_group = models.ForeignKey("selling.CustomerGroup", models.CASCADE, null=True, blank=True,
                                       related_name="+", verbose_name="لفئة عملاء")
    supplier = models.ForeignKey("buying.Supplier", models.CASCADE, null=True, blank=True, related_name="+",
                                 verbose_name="لمورد محدد")
    min_qty = models.DecimalField("أقل كمية", max_digits=18, decimal_places=3, default=0,
                                  help_text="بالوحدة الأساسية للصنف في السطر. صفر = أي كمية")
    max_qty = models.DecimalField("أكبر كمية", max_digits=18, decimal_places=3, default=0, help_text="صفر = بلا حد")
    valid_from = models.DateField("ساري من", null=True, blank=True)
    valid_upto = models.DateField("ساري حتى", null=True, blank=True)
    channel = models.CharField("القناة", max_length=12, choices=Channel.choices, default=Channel.ALL)
    discount_type = models.CharField("نوع الخصم", max_length=12, choices=DiscountType.choices,
                                     default=DiscountType.PERCENTAGE)
    discount_percentage = models.DecimalField("نسبة الخصم %", max_digits=6, decimal_places=2, default=0)
    discount_amount = models.DecimalField("مبلغ الخصم للوحدة", max_digits=18, decimal_places=2, default=0)
    rate = models.DecimalField("السعر الخاص للوحدة", max_digits=18, decimal_places=2, default=0)
    free_item = models.ForeignKey("stock.Item", models.CASCADE, null=True, blank=True, related_name="+",
                                  verbose_name="صنف الهدية", help_text="اشترِ X واحصل على Y: يُضاف سطر بسعر صفر")
    free_qty = models.DecimalField("كمية الهدية", max_digits=18, decimal_places=3, default=0)
    recurse = models.BooleanField("تتكرر الهدية لكل مضاعف من أقل كمية", default=True,
                                  help_text="مثال: اشترِ 2 واحصل على 1 — شراء 6 يعطي 3 هدايا")
    priority = models.PositiveSmallIntegerField("الأولوية", default=1, help_text="الأعلى يُطبق أولاً عند تعارض أكثر من قاعدة")
    coupon_code = models.CharField("كود الكوبون", max_length=40, blank=True,
                                   help_text="إن حُدد لا تُطبق القاعدة إلا بإدخال الكود في المستند")
    max_uses = models.PositiveIntegerField("أقصى عدد استخدامات للكوبون", default=0, help_text="صفر = بلا حد")
    used_count = models.PositiveIntegerField("مرات الاستخدام", default=0, editable=False)
    disabled = models.BooleanField("موقوفة", default=False)

    class Meta:
        verbose_name = "قاعدة تسعير / عرض"
        verbose_name_plural = "قواعد التسعير والعروض"
        ordering = ["-priority", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        errors = {}
        if self.apply_on == ApplyOn.ITEM and not self.item_id:
            errors["item"] = "حدد الصنف."
        if self.apply_on == ApplyOn.ITEM_GROUP and not self.item_group_id:
            errors["item_group"] = "حدد مجموعة الأصناف."
        if self.customer_id and self.customer_group_id:
            errors["customer_group"] = "حدد عميلاً أو فئة عملاء، لا كليهما."
        if self.kind == PriceKind.BUYING and (self.customer_id or self.customer_group_id or self.coupon_code):
            errors["kind"] = "قواعد الشراء تُحدد بالمورد فقط (بلا عملاء أو كوبونات)."
        if self.kind == PriceKind.SELLING and self.supplier_id:
            errors["supplier"] = "المورد لقواعد الشراء فقط."
        if self.discount_type == DiscountType.PERCENTAGE and not (0 < self.discount_percentage <= 100):
            errors["discount_percentage"] = "أدخل نسبة بين 0 و100."
        if self.discount_type == DiscountType.AMOUNT and self.discount_amount <= 0:
            errors["discount_amount"] = "أدخل مبلغ الخصم."
        if self.discount_type == DiscountType.RATE and self.rate < 0:
            errors["rate"] = "السعر لا يكون سالباً."
        if self.discount_type == DiscountType.NONE and not self.free_item_id:
            errors["free_item"] = "حدد صنف الهدية أو اختر نوع خصم."
        if self.free_item_id and self.free_qty <= 0:
            errors["free_qty"] = "أدخل كمية الهدية."
        if self.free_item_id and self.recurse and self.min_qty <= 0:
            errors["min_qty"] = "لتكرار الهدية حدد أقل كمية (مثل 2 في «اشترِ 2 واحصل على 1»)."
        if self.max_qty and self.max_qty < self.min_qty:
            errors["max_qty"] = "أكبر كمية أقل من أقل كمية."
        if self.valid_from and self.valid_upto and self.valid_from > self.valid_upto:
            errors["valid_upto"] = "تاريخ النهاية قبل تاريخ البداية."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.coupon_code = self.coupon_code.strip().upper()
        super().save(*args, **kwargs)

    @property
    def coupon_exhausted(self):
        return bool(self.coupon_code and self.max_uses and self.used_count >= self.max_uses)


class LoyaltyProgram(TenantModel):
    """برنامج ولاء: نقاط على المشتريات تُستبدل خصماً على الفواتير اللاحقة."""

    name = models.CharField("اسم البرنامج", max_length=100)
    amount_per_point = models.DecimalField("المبلغ المطلوب لكل نقطة", max_digits=18, decimal_places=2, default=10,
                                           help_text="مثل 10: كل 10 ريالات من الفاتورة (شاملة الضريبة) = نقطة")
    point_value = models.DecimalField("قيمة النقطة عند الاستبدال", max_digits=18, decimal_places=4, default="0.1",
                                      help_text="مثل 0.10 ريال لكل نقطة")
    min_redeem_points = models.PositiveIntegerField("أقل عدد نقاط للاستبدال", default=0)
    expiry_days = models.PositiveIntegerField("صلاحية النقاط بالأيام", default=0, help_text="صفر = لا تنتهي")
    expense_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, null=True, blank=True, related_name="+",
        limit_choices_to={"is_group": False, "root_type": "expense"}, verbose_name="حساب مصروف الولاء",
        help_text="يُحمّل عليه قيمة النقاط المستبدلة. فارغ = حساب الخصم المسموح به")
    auto_enroll = models.BooleanField("تسجيل كل العملاء تلقائياً", default=False,
                                      help_text="إلا العميل النقدي الافتراضي لنقاط البيع. وإلا يُحدد البرنامج في بطاقة العميل")
    disabled = models.BooleanField("موقوف", default=False)

    class Meta:
        verbose_name = "برنامج ولاء"
        verbose_name_plural = "برامج الولاء"
        constraints = [tenant_unique("name", model_label="loyaltyprogram")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.amount_per_point is not None and self.amount_per_point <= 0:
            raise ValidationError({"amount_per_point": "أدخل مبلغاً أكبر من صفر."})
        if self.point_value is not None and self.point_value <= 0:
            raise ValidationError({"point_value": "أدخل قيمة أكبر من صفر."})


class LoyaltyPointEntry(TenantModel):
    """حركة نقاط ولاء: موجبة مكتسبة من فاتورة، وسالبة مستبدلة أو مخصومة بمرتجع."""

    customer = models.ForeignKey("selling.Customer", models.CASCADE, related_name="loyalty_entries",
                                 verbose_name="العميل")
    program = models.ForeignKey(LoyaltyProgram, models.PROTECT, related_name="entries", verbose_name="البرنامج")
    invoice = models.ForeignKey("selling.SalesInvoice", models.CASCADE, related_name="loyalty_entries",
                                verbose_name="الفاتورة")
    points = models.IntegerField("النقاط")
    posting_date = models.DateField("التاريخ", default=timezone.localdate)
    expiry_date = models.DateField("تنتهي في", null=True, blank=True)

    class Meta:
        verbose_name = "حركة نقاط ولاء"
        verbose_name_plural = "حركات نقاط الولاء"
        ordering = ["-posting_date", "-id"]

    def __str__(self):
        return f"{self.customer}: {self.points}"
