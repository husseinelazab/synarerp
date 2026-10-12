"""الجودة: نماذج فحص بمعايير قبول، وفحص إلزامي عند الاستلام أو الإنتاج يمنع إدخال المرفوض للمخزون."""
from django.core.exceptions import ValidationError
from django.db import models

from core.models import SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class QualityTemplate(TenantModel):
    name = models.CharField("اسم نموذج الفحص", max_length=100)
    notes = models.TextField("تعليمات الفحص", blank=True)

    class Meta:
        verbose_name = "نموذج فحص جودة"
        verbose_name_plural = "نماذج فحص الجودة"
        constraints = [tenant_unique("name", model_label="qualitytemplate")]

    def __str__(self):
        return self.name


class QualityParameter(models.Model):
    parent = models.ForeignKey(QualityTemplate, models.CASCADE, related_name="parameters")
    name = models.CharField("المعيار", max_length=100, help_text="مثل: الرطوبة %، الوزن، المظهر، سلامة التغليف")
    min_value = models.DecimalField("الحد الأدنى", max_digits=12, decimal_places=3, null=True, blank=True)
    max_value = models.DecimalField("الحد الأعلى", max_digits=12, decimal_places=3, null=True, blank=True)
    criteria = models.CharField("معيار القبول (وصفي)", max_length=200, blank=True,
                                help_text="للمعايير غير الرقمية؛ يحكم الفاحص بالقبول أو الرفض")

    class Meta:
        verbose_name = "معيار فحص"
        verbose_name_plural = "معايير الفحص"


class InspectionStage(models.TextChoices):
    RECEIPT = "receipt", "عند الاستلام من المورد"
    PRODUCTION = "production", "عند الإنتاج"


class InspectionResult(models.TextChoices):
    PENDING = "pending", "قيد الفحص"
    ACCEPTED = "accepted", "مقبول"
    REJECTED = "rejected", "مرفوض"


class QualityInspection(SubmittableDocument):
    naming_prefix = "QI"
    posts_to_ledger = False
    posting_note = "الفحص المعتمد بنتيجة «مقبول» يسمح باعتماد المستند المرتبط وإدخال الصنف للمخزون."

    stage = models.CharField("المرحلة", max_length=12, choices=InspectionStage.choices, default=InspectionStage.RECEIPT)
    item = models.ForeignKey("stock.Item", models.PROTECT, related_name="+", verbose_name="الصنف")
    template = models.ForeignKey(QualityTemplate, models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="نموذج الفحص")
    purchase_receipt = models.ForeignKey("buying.PurchaseReceipt", models.CASCADE, null=True, blank=True,
                                         related_name="inspections", verbose_name="إذن الاستلام")
    purchase_invoice = models.ForeignKey("buying.PurchaseInvoice", models.CASCADE, null=True, blank=True,
                                         related_name="inspections", verbose_name="فاتورة المشتريات")
    production = models.ForeignKey("manufacturing.Production", models.CASCADE, null=True, blank=True,
                                   related_name="inspections", verbose_name="أمر الإنتاج")
    sample_size = models.DecimalField("حجم العينة", max_digits=12, decimal_places=3, default=1)
    result = models.CharField("النتيجة", max_length=10, choices=InspectionResult.choices,
                              default=InspectionResult.PENDING, editable=False)
    inspector_notes = models.TextField("ملاحظات الفاحص", blank=True)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "فحص جودة"
        verbose_name_plural = "فحوصات الجودة"

    @property
    def reference(self):
        return self.purchase_receipt or self.purchase_invoice or self.production

    @property
    def linked_documents(self):
        return [self.reference] if self.reference else []

    @property
    def status(self):
        return self.result

    def get_status_display(self):
        return self.get_result_display()

    def calculate_totals(self):
        if self.is_draft and self.pk and not self.readings.exists():
            template = self.template or self.item.quality_template
            if template is not None:
                self.template = template
                for p in template.parameters.all():
                    self.readings.create(parameter=p.name, min_value=p.min_value, max_value=p.max_value,
                                         criteria=p.criteria)
        readings = list(self.readings.all())
        for r in readings:
            r.evaluate()
            r.save(update_fields=["passed"])
        if readings and all(r.passed is not None for r in readings):
            self.result = InspectionResult.ACCEPTED if all(r.passed for r in readings) else InspectionResult.REJECTED
        else:
            self.result = InspectionResult.PENDING

    def validate_for_submit(self):
        if self.reference is None:
            raise ValidationError("اربط الفحص بإذن استلام أو فاتورة مشتريات أو أمر إنتاج.")
        if self.result == InspectionResult.PENDING:
            raise ValidationError("أكمل قراءات كل المعايير (قيمة رقمية أو قبول/رفض للوصفي).")


class QualityReading(models.Model):
    parent = models.ForeignKey(QualityInspection, models.CASCADE, related_name="readings")
    parameter = models.CharField("المعيار", max_length=100)
    min_value = models.DecimalField("الحد الأدنى", max_digits=12, decimal_places=3, null=True, blank=True)
    max_value = models.DecimalField("الحد الأعلى", max_digits=12, decimal_places=3, null=True, blank=True)
    criteria = models.CharField("معيار القبول", max_length=200, blank=True)
    value = models.DecimalField("القراءة", max_digits=12, decimal_places=3, null=True, blank=True)
    manual_result = models.CharField("حكم الفاحص", max_length=10, blank=True,
                                     choices=[("ok", "مطابق"), ("fail", "غير مطابق")])
    passed = models.BooleanField("مطابق", null=True, editable=False)

    class Meta:
        verbose_name = "قراءة فحص"
        verbose_name_plural = "القراءات"

    def evaluate(self):
        if self.manual_result:
            self.passed = self.manual_result == "ok"
        elif self.value is not None and (self.min_value is not None or self.max_value is not None):
            self.passed = ((self.min_value is None or flt(self.value, 3) >= flt(self.min_value, 3)) and
                           (self.max_value is None or flt(self.value, 3) <= flt(self.max_value, 3)))
        else:
            self.passed = None


def require_inspections(doc, items, stage):
    """قبل اعتماد استلام/إنتاج: كل صنف يتطلب فحصاً يجب أن يكون له فحص معتمد مقبول مرتبط بالمستند."""
    field = {"purchasereceipt": "purchase_receipt", "purchaseinvoice": "purchase_invoice",
             "production": "production"}[doc._meta.model_name]
    for item in items:
        required = item.inspection_on_receipt if stage == InspectionStage.RECEIPT else item.inspection_on_production
        if not required:
            continue
        inspections = QualityInspection.objects.filter(**{field: doc}, item=item, docstatus=1)
        if inspections.filter(result=InspectionResult.REJECTED).exists():
            raise ValidationError(f"«{item.name}» مرفوض في فحص الجودة؛ أعده للمورد أو احذفه من المستند.")
        if not inspections.filter(result=InspectionResult.ACCEPTED).exists():
            raise ValidationError(f"«{item.name}» يتطلب فحص جودة مقبولاً قبل الاعتماد — «إنشاء فحص جودة» من صفحة المستند.")


def pending_items(doc, items, stage):
    field = {"purchasereceipt": "purchase_receipt", "purchaseinvoice": "purchase_invoice",
             "production": "production"}[doc._meta.model_name]
    done = set(QualityInspection.objects.filter(**{field: doc}).exclude(docstatus=2).values_list("item_id", flat=True))
    flag = "inspection_on_receipt" if stage == InspectionStage.RECEIPT else "inspection_on_production"
    return [i for i in items if getattr(i, flag) and i.pk not in done]


def make_inspection(doc, items, stage, user=None):
    """مسودة فحص لأول صنف يتطلب فحصاً ولم يُفحص بعد في المستند."""
    pending = pending_items(doc, items, stage)
    if not pending:
        raise ValidationError("لا توجد أصناف تحتاج فحص جودة في هذا المستند.")
    field = {"purchasereceipt": "purchase_receipt", "purchaseinvoice": "purchase_invoice",
             "production": "production"}[doc._meta.model_name]
    inspection = QualityInspection.objects.create(stage=stage, item=pending[0], created_by=user, **{field: doc},
                                                  template=pending[0].quality_template)
    inspection.calculate_totals()
    inspection.save()
    return inspection
