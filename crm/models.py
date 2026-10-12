"""إدارة علاقات العملاء: العملاء المحتملون، فرص البيع، والمتابعات."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.models import TenantModel


class LeadSource(models.TextChoices):
    WEBSITE = "website", "الموقع الإلكتروني"
    SOCIAL = "social", "وسائل التواصل"
    REFERRAL = "referral", "توصية"
    CALL = "call", "اتصال وارد"
    EXHIBITION = "exhibition", "معرض"
    CAMPAIGN = "campaign", "حملة تسويقية"
    OTHER = "other", "أخرى"


class LeadStatus(models.TextChoices):
    NEW = "new", "جديد"
    CONTACTED = "contacted", "تم التواصل"
    QUALIFIED = "qualified", "مؤهل"
    CONVERTED = "converted", "تحول لعميل"
    LOST = "lost", "غير مهتم"


class Lead(TenantModel):
    name = models.CharField("الاسم", max_length=150)
    organization = models.CharField("المنشأة", max_length=150, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=100, blank=True)
    source = models.CharField("المصدر", max_length=12, choices=LeadSource.choices, default=LeadSource.OTHER)
    status = models.CharField("الحالة", max_length=12, choices=LeadStatus.choices, default=LeadStatus.NEW)
    sales_person = models.ForeignKey("selling.SalesPerson", models.SET_NULL, null=True, blank=True, related_name="+",
                                     verbose_name="المندوب")
    territory = models.ForeignKey("selling.Territory", models.SET_NULL, null=True, blank=True, related_name="+",
                                  verbose_name="المنطقة")
    interest = models.TextField("الاهتمام / الاحتياج", blank=True)
    customer = models.ForeignKey("selling.Customer", models.SET_NULL, null=True, blank=True, related_name="leads",
                                 editable=False, verbose_name="العميل بعد التحويل")
    created_at = models.DateTimeField("تاريخ الإضافة", auto_now_add=True)

    class Meta:
        verbose_name = "عميل محتمل"
        verbose_name_plural = "العملاء المحتملون"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} — {self.organization}" if self.organization else self.name

    @property
    def available_actions(self):
        actions = [("make_opportunity", "إنشاء فرصة بيع"), ("log_call", "تسجيل متابعة")]
        if not self.customer_id:
            actions.insert(0, ("convert_to_customer", "تحويل إلى عميل"))
        if self.status not in (LeadStatus.LOST, LeadStatus.CONVERTED):
            actions.append(("mark_lost", "غير مهتم"))
        return actions

    def convert_to_customer(self, user=None):
        from selling.models import Customer

        if self.customer_id:
            return self.customer
        name = self.organization or self.name
        base, n = name, 1
        while Customer.objects.filter(name=name).exists():
            n += 1
            name = f"{base} ({n})"
        with transaction.atomic():
            self.customer = Customer.objects.create(
                name=name, phone=self.phone, email=self.email, city=self.city,
                customer_type="company" if self.organization else "individual",
                sales_person=self.sales_person, territory=self.territory)
            self.status = LeadStatus.CONVERTED
            self.save(update_fields=["customer", "status"])
            self.opportunities.filter(customer__isnull=True).update(customer=self.customer)
        return None  # يبقى في صفحة العميل المحتمل

    def make_opportunity(self, user=None):
        if self.status == LeadStatus.NEW:
            self.status = LeadStatus.QUALIFIED
            self.save(update_fields=["status"])
        opp = Opportunity.objects.create(title=f"فرصة — {self}", lead=self, customer=self.customer,
                                         sales_person=self.sales_person, owner=user)
        return opp

    def log_call(self, user=None):
        Activity.objects.create(lead=self, kind=ActivityKind.CALL, summary="اتصال", assigned_to=user, done=True,
                                date=timezone.localdate())
        if self.status == LeadStatus.NEW:
            self.status = LeadStatus.CONTACTED
            self.save(update_fields=["status"])

    def mark_lost(self, user=None):
        self.status = LeadStatus.LOST
        self.save(update_fields=["status"])


class OpportunityStage(models.TextChoices):
    PROSPECTING = "prospecting", "استكشاف"
    PROPOSAL = "proposal", "عرض سعر"
    NEGOTIATION = "negotiation", "تفاوض"
    WON = "won", "مكسوبة"
    LOST = "lost", "خاسرة"


STAGE_PROBABILITY = {"prospecting": 10, "proposal": 40, "negotiation": 70, "won": 100, "lost": 0}


class Opportunity(TenantModel):
    title = models.CharField("العنوان", max_length=200)
    lead = models.ForeignKey(Lead, models.SET_NULL, null=True, blank=True, related_name="opportunities",
                             verbose_name="العميل المحتمل")
    customer = models.ForeignKey("selling.Customer", models.SET_NULL, null=True, blank=True,
                                 related_name="opportunities", verbose_name="العميل")
    stage = models.CharField("المرحلة", max_length=12, choices=OpportunityStage.choices,
                             default=OpportunityStage.PROSPECTING)
    expected_amount = models.DecimalField("القيمة المتوقعة", max_digits=18, decimal_places=2, default=0)
    probability = models.PositiveSmallIntegerField("احتمال الفوز %", default=10)
    expected_close = models.DateField("تاريخ الإغلاق المتوقع", null=True, blank=True)
    sales_person = models.ForeignKey("selling.SalesPerson", models.SET_NULL, null=True, blank=True, related_name="+",
                                     verbose_name="المندوب")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                              verbose_name="المسؤول")
    quotation = models.ForeignKey("selling.Quotation", models.SET_NULL, null=True, blank=True, related_name="+",
                                  editable=False, verbose_name="عرض السعر")
    lost_reason = models.CharField("سبب الخسارة", max_length=200, blank=True)
    notes = models.TextField("ملاحظات", blank=True)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    closed_on = models.DateField("تاريخ الإغلاق", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "فرصة بيع"
        verbose_name_plural = "فرص البيع"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def weighted_amount(self):
        return self.expected_amount * self.probability / 100

    def save(self, *args, **kwargs):
        if self.stage in (OpportunityStage.WON, OpportunityStage.LOST):
            self.closed_on = self.closed_on or timezone.localdate()
            self.probability = STAGE_PROBABILITY[self.stage]
        else:
            self.closed_on = None
        super().save(*args, **kwargs)

    @property
    def available_actions(self):
        if self.stage in (OpportunityStage.WON, OpportunityStage.LOST):
            return [("reopen", "إعادة فتح")]
        actions = [("make_quotation", "إنشاء عرض سعر"), ("mark_won", "تم الفوز"), ("mark_lost", "خسارة")]
        if self.quotation_id:
            actions.pop(0)
        return actions

    @property
    def linked_documents(self):
        return [self.quotation] if self.quotation_id else []

    def _customer(self):
        if self.customer_id:
            return self.customer
        if self.lead_id:
            self.lead.convert_to_customer()
            self.lead.refresh_from_db()
            self.customer = self.lead.customer
            self.save(update_fields=["customer"])
            return self.customer
        raise ValidationError("حدد العميل أو العميل المحتمل أولاً.")

    def make_quotation(self, user=None):
        from selling.models import Quotation

        customer = self._customer()
        quotation = Quotation.objects.create(customer=customer, created_by=user, sales_person=self.sales_person,
                                             remarks=f"من فرصة البيع: {self.title}")
        self.quotation, self.stage = quotation, OpportunityStage.PROPOSAL
        self.probability = max(self.probability, STAGE_PROBABILITY["proposal"])
        self.save()
        return quotation

    def _set_stage(self, stage):
        self.stage = stage
        self.save()

    def mark_won(self, user=None):
        self._customer() if self.lead_id or self.customer_id else None
        self._set_stage(OpportunityStage.WON)

    def mark_lost(self, user=None):
        self._set_stage(OpportunityStage.LOST)

    def reopen(self, user=None):
        self.stage = OpportunityStage.NEGOTIATION
        self.probability = STAGE_PROBABILITY["negotiation"]
        self.save()


class ActivityKind(models.TextChoices):
    CALL = "call", "اتصال"
    MEETING = "meeting", "اجتماع / زيارة"
    EMAIL = "email", "بريد"
    WHATSAPP = "whatsapp", "واتساب"
    TASK = "task", "مهمة"


class Activity(TenantModel):
    """متابعة (اتصال، زيارة، مهمة) مع موعد يُنبَّه به المسؤول."""

    kind = models.CharField("النوع", max_length=10, choices=ActivityKind.choices, default=ActivityKind.CALL)
    summary = models.CharField("الموضوع", max_length=200)
    details = models.TextField("التفاصيل / النتيجة", blank=True)
    date = models.DateField("التاريخ", default=timezone.localdate)
    lead = models.ForeignKey(Lead, models.CASCADE, null=True, blank=True, related_name="activities",
                             verbose_name="العميل المحتمل")
    opportunity = models.ForeignKey(Opportunity, models.CASCADE, null=True, blank=True, related_name="activities",
                                    verbose_name="فرصة البيع")
    customer = models.ForeignKey("selling.Customer", models.CASCADE, null=True, blank=True, related_name="activities",
                                 verbose_name="العميل")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="المسؤول")
    done = models.BooleanField("منجزة", default=False)
    reminded = models.BooleanField(default=False, editable=False)

    class Meta:
        verbose_name = "متابعة"
        verbose_name_plural = "المتابعات"
        ordering = ["done", "date"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.summary}"

    @property
    def regarding(self):
        return self.opportunity or self.lead or self.customer

    @property
    def available_actions(self):
        return [] if self.done else [("mark_done", "تمت")]

    def mark_done(self, user=None):
        self.done = True
        self.save(update_fields=["done"])
