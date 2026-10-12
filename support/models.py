"""الصيانة وخدمة العملاء: تذاكر الأعطال والطلبات بمستوى خدمة، وعقود الصيانة وزياراتها."""
import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TenantModel

SLA_HOURS = {"urgent": 4, "high": 24, "normal": 72, "low": 168}


class Priority(models.TextChoices):
    URGENT = "urgent", "عاجلة (4 ساعات)"
    HIGH = "high", "عالية (يوم)"
    NORMAL = "normal", "عادية (3 أيام)"
    LOW = "low", "منخفضة (أسبوع)"


class TicketStatus(models.TextChoices):
    OPEN = "open", "مفتوحة"
    IN_PROGRESS = "in_progress", "قيد المعالجة"
    WAITING = "waiting", "بانتظار العميل / قطع الغيار"
    RESOLVED = "resolved", "تم الحل"
    CLOSED = "closed", "مغلقة"


class TicketKind(models.TextChoices):
    REPAIR = "repair", "عطل / إصلاح"
    INSTALLATION = "installation", "تركيب"
    INQUIRY = "inquiry", "استفسار"
    COMPLAINT = "complaint", "شكوى"


class ServiceTicket(TenantModel):
    subject = models.CharField("الموضوع", max_length=200)
    kind = models.CharField("النوع", max_length=15, choices=TicketKind.choices, default=TicketKind.REPAIR)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, related_name="tickets", verbose_name="العميل")
    contact_phone = models.CharField("جوال التواصل", max_length=30, blank=True)
    serial_no = models.ForeignKey("stock.SerialNo", models.SET_NULL, null=True, blank=True, related_name="tickets",
                                  verbose_name="الرقم التسلسلي للجهاز")
    item = models.ForeignKey("stock.Item", models.SET_NULL, null=True, blank=True, related_name="+",
                             verbose_name="الصنف / الجهاز")
    contract = models.ForeignKey("support.MaintenanceContract", models.SET_NULL, null=True, blank=True,
                                 related_name="tickets", verbose_name="عقد الصيانة")
    description = models.TextField("وصف المشكلة", blank=True)
    priority = models.CharField("الأولوية", max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField("الحالة", max_length=12, choices=TicketStatus.choices, default=TicketStatus.OPEN)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="الفني المسؤول")
    opened_at = models.DateTimeField("وقت الفتح", default=timezone.now)
    due_at = models.DateTimeField("موعد الحل المستهدف", null=True, blank=True, editable=False)
    resolved_at = models.DateTimeField("وقت الحل", null=True, blank=True, editable=False)
    resolution = models.TextField("الحل / التقرير الفني", blank=True)
    covered = models.BooleanField("مشمولة بالضمان أو العقد", default=False, editable=False)
    invoice = models.ForeignKey("selling.SalesInvoice", models.SET_NULL, null=True, blank=True, related_name="+",
                                editable=False, verbose_name="فاتورة الصيانة")

    class Meta:
        verbose_name = "تذكرة صيانة / خدمة"
        verbose_name_plural = "تذاكر الصيانة والخدمة"
        ordering = ["-opened_at"]

    def __str__(self):
        return f"#{self.pk} {self.subject}"

    def save(self, *args, **kwargs):
        if self.serial_no_id:
            self.item = self.item or self.serial_no.item
        self.due_at = self.opened_at + datetime.timedelta(hours=SLA_HOURS[self.priority])
        self.covered = self.coverage() is not None
        if self.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            self.resolved_at = self.resolved_at or timezone.now()
        else:
            self.resolved_at = None
        super().save(*args, **kwargs)

    def coverage(self):
        """سبب التغطية المجانية (ضمان الجهاز أو عقد ساري) أو None."""
        today = timezone.localdate()
        if self.serial_no_id and self.serial_no.under_warranty:
            return f"ضمان حتى {self.serial_no.warranty_expiry:%Y-%m-%d}"
        contract = self.contract or MaintenanceContract.objects.filter(
            customer_id=self.customer_id, start_date__lte=today, end_date__gte=today,
            status=ContractStatus.ACTIVE).first()
        if contract is not None and contract.is_active:
            return f"عقد صيانة {contract}"
        return None

    @property
    def is_overdue(self):
        return self.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED) and self.due_at and \
            self.due_at < timezone.now()

    @property
    def met_sla(self):
        return bool(self.resolved_at and self.due_at and self.resolved_at <= self.due_at)

    @property
    def available_actions(self):
        actions = []
        if self.status in (TicketStatus.OPEN, TicketStatus.WAITING):
            actions.append(("start", "بدء المعالجة"))
        if self.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            actions.append(("resolve", "تم الحل"))
        if self.status == TicketStatus.RESOLVED:
            actions.append(("close", "إغلاق"))
        if not self.covered and not self.invoice_id:
            actions.append(("make_invoice", "إنشاء فاتورة صيانة"))
        return actions

    def start(self, user=None):
        self.status = TicketStatus.IN_PROGRESS
        if not self.assigned_to_id:
            self.assigned_to = user
        self.save()

    def resolve(self, user=None):
        self.status = TicketStatus.RESOLVED
        self.save()

    def close(self, user=None):
        self.status = TicketStatus.CLOSED
        self.save()

    def make_invoice(self, user=None):
        from selling.models import SalesInvoice

        if self.covered:
            raise ValidationError(f"التذكرة مشمولة ({self.coverage()}) ولا تُفوتر.")
        invoice = SalesInvoice.objects.create(customer=self.customer, created_by=user, update_stock=True,
                                              remarks=f"صيانة — تذكرة {self}")
        self.invoice = invoice
        self.save(update_fields=["invoice"])
        return invoice


class ContractStatus(models.TextChoices):
    ACTIVE = "active", "ساري"
    SUSPENDED = "suspended", "موقوف"
    ENDED = "ended", "منتهٍ"


class MaintenanceContract(TenantModel):
    """عقد صيانة دورية لعميل: مدة وقيمة وعدد زيارات تُجدول تلقائياً، وتجعل تذاكره مشمولة."""

    name = models.CharField("اسم العقد", max_length=150)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, related_name="maintenance_contracts",
                                 verbose_name="العميل")
    start_date = models.DateField("البداية")
    end_date = models.DateField("النهاية")
    amount = models.DecimalField("قيمة العقد", max_digits=18, decimal_places=2, default=0)
    visits_per_year = models.PositiveSmallIntegerField("الزيارات الدورية في السنة", default=4)
    scope = models.TextField("نطاق العقد (الأجهزة والخدمات المشمولة)", blank=True)
    technician = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                   verbose_name="الفني")
    status = models.CharField("الحالة", max_length=10, choices=ContractStatus.choices, default=ContractStatus.ACTIVE)

    class Meta:
        verbose_name = "عقد صيانة"
        verbose_name_plural = "عقود الصيانة"
        ordering = ["end_date"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "النهاية قبل البداية."})

    @property
    def is_active(self):
        today = timezone.localdate()
        return self.status == ContractStatus.ACTIVE and self.start_date <= today <= self.end_date

    @property
    def available_actions(self):
        return [] if self.visits.exists() else [("schedule_visits", "جدولة الزيارات الدورية")]

    def schedule_visits(self, user=None):
        """زيارات موزعة بالتساوي على مدة العقد."""
        if not self.visits_per_year:
            raise ValidationError("حدد عدد الزيارات.")
        days = (self.end_date - self.start_date).days + 1
        count = max(round(self.visits_per_year * days / 365), 1)
        step = days / count
        for i in range(count):
            MaintenanceVisit.objects.create(contract=self, customer=self.customer, technician=self.technician,
                                            scheduled_date=self.start_date + datetime.timedelta(days=int(step * i + step / 2)),
                                            purpose=f"زيارة دورية {i + 1} من {count}")


class MaintenanceVisit(TenantModel):
    contract = models.ForeignKey(MaintenanceContract, models.CASCADE, null=True, blank=True, related_name="visits",
                                 verbose_name="العقد")
    ticket = models.ForeignKey(ServiceTicket, models.CASCADE, null=True, blank=True, related_name="visits",
                               verbose_name="التذكرة")
    customer = models.ForeignKey("selling.Customer", models.PROTECT, related_name="+", verbose_name="العميل")
    scheduled_date = models.DateField("موعد الزيارة")
    technician = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                   verbose_name="الفني")
    purpose = models.CharField("الغرض", max_length=200)
    report = models.TextField("تقرير الزيارة", blank=True)
    done = models.BooleanField("تمت", default=False)
    reminded = models.BooleanField(default=False, editable=False)

    class Meta:
        verbose_name = "زيارة صيانة"
        verbose_name_plural = "زيارات الصيانة"
        ordering = ["done", "scheduled_date"]

    def __str__(self):
        return f"{self.customer} — {self.scheduled_date:%Y-%m-%d}"

    @property
    def available_actions(self):
        return [] if self.done else [("mark_done", "تمت الزيارة")]

    def mark_done(self, user=None):
        self.done = True
        self.save(update_fields=["done"])
