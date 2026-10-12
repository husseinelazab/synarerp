"""المشاريع: المهام ونسبة الإنجاز، وتكاليف وإيرادات كل مشروع من القيود، والفوترة حسب نسبة الإنجاز."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum

from core.models import TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class ProjectStatus(models.TextChoices):
    PLANNED = "planned", "مخطط"
    ACTIVE = "active", "قيد التنفيذ"
    ON_HOLD = "on_hold", "متوقف"
    COMPLETED = "completed", "منجز"
    CANCELLED = "cancelled", "ملغى"


class Project(TenantModel):
    name = models.CharField("اسم المشروع", max_length=150)
    customer = models.ForeignKey("selling.Customer", models.PROTECT, null=True, blank=True, related_name="projects",
                                 verbose_name="العميل")
    status = models.CharField("الحالة", max_length=10, choices=ProjectStatus.choices, default=ProjectStatus.ACTIVE)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="مدير المشروع")
    start_date = models.DateField("تاريخ البدء", null=True, blank=True)
    end_date = models.DateField("تاريخ الانتهاء المخطط", null=True, blank=True)
    contract_value = models.DecimalField("قيمة العقد (قبل الضريبة)", max_digits=18, decimal_places=2, default=0)
    cost_budget = models.DecimalField("موازنة التكاليف", max_digits=18, decimal_places=2, default=0)
    billing_item = models.ForeignKey("stock.Item", models.PROTECT, null=True, blank=True, related_name="+",
                                     limit_choices_to={"is_stock_item": False}, verbose_name="صنف الفوترة",
                                     help_text="صنف خدمي تُصدر به مستخلصات المشروع حسب نسبة الإنجاز")
    progress_source = models.CharField("نسبة الإنجاز من", max_length=10, default="tasks",
                                       choices=[("tasks", "المهام (متوسط موزون)"), ("manual", "إدخال يدوي")])
    manual_progress = models.DecimalField("نسبة الإنجاز اليدوية %", max_digits=5, decimal_places=2, default=0)
    description = models.TextField("الوصف", blank=True)

    class Meta:
        verbose_name = "مشروع"
        verbose_name_plural = "المشاريع"
        ordering = ["-id"]
        constraints = [tenant_unique("name", model_label="project")]

    def __str__(self):
        return self.name

    @property
    def progress(self):
        if self.progress_source == "manual":
            return flt(self.manual_progress)
        tasks = list(self.tasks.exclude(status=TaskStatus.CANCELLED))
        weight = sum((flt(t.weight) for t in tasks), flt(0))
        if not weight:
            return flt(0)
        return flt(sum((flt(t.weight) * flt(t.effective_progress) for t in tasks), flt(0)) / weight)

    def gl_totals(self):
        from accounts.models import GLEntry, RootType

        rows = (GLEntry.objects.filter(project=self, is_cancelled=False, account__root_type__in=[RootType.INCOME,
                                                                                                RootType.EXPENSE])
                .values("account__root_type").annotate(d=Sum("debit"), c=Sum("credit")))
        totals = {r["account__root_type"]: (flt(r["d"]), flt(r["c"])) for r in rows}
        income = totals.get(RootType.INCOME, (0, 0))
        expense = totals.get(RootType.EXPENSE, (0, 0))
        revenue, cost = flt(income[1] - income[0]), flt(expense[0] - expense[1])
        return {"revenue": revenue, "cost": cost, "profit": revenue - cost,
                "margin": flt((revenue - cost) * 100 / revenue) if revenue else None}

    def billed_amount(self):
        from selling.models import SalesInvoice

        return flt(SalesInvoice.objects.filter(project=self, docstatus=1).aggregate(t=Sum("base_net_total"))["t"])

    @property
    def available_actions(self):
        actions = [("make_progress_invoice", "إصدار مستخلص حسب نسبة الإنجاز")] if self.contract_value else []
        if self.status != ProjectStatus.COMPLETED:
            actions.append(("mark_completed", "إنهاء المشروع"))
        return actions

    def make_progress_invoice(self, user=None):
        from selling.models import SalesInvoice

        if not self.customer_id or not self.billing_item_id:
            raise ValidationError("حدد العميل وصنف الفوترة في المشروع أولاً.")
        due = flt(flt(self.contract_value) * self.progress / 100) - self.billed_amount()
        if due <= 0:
            raise ValidationError(f"لا يوجد مستحق للفوترة: نسبة الإنجاز {self.progress}% وقد فُوتر {self.billed_amount():,.2f}.")
        with transaction.atomic():
            invoice = SalesInvoice.objects.create(customer=self.customer, project=self, update_stock=False,
                                                  created_by=user,
                                                  remarks=f"مستخلص مشروع {self.name} — نسبة الإنجاز {self.progress}%")
            invoice.items.create(item=self.billing_item, qty=1, rate=due)
            invoice.calculate_totals()
            invoice.save()
        return invoice

    def mark_completed(self, user=None):
        self.status = ProjectStatus.COMPLETED
        self.save(update_fields=["status"])


class TaskStatus(models.TextChoices):
    OPEN = "open", "لم تبدأ"
    WORKING = "working", "جارية"
    DONE = "done", "منجزة"
    CANCELLED = "cancelled", "ملغاة"


class ProjectTask(TenantModel):
    project = models.ForeignKey(Project, models.CASCADE, related_name="tasks", verbose_name="المشروع")
    title = models.CharField("المهمة", max_length=200)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="المسؤول")
    start_date = models.DateField("البداية", null=True, blank=True)
    due_date = models.DateField("الاستحقاق", null=True, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    progress = models.DecimalField("نسبة الإنجاز %", max_digits=5, decimal_places=2, default=0)
    weight = models.DecimalField("الوزن", max_digits=8, decimal_places=2, default=1,
                                 help_text="أهمية المهمة في حساب نسبة إنجاز المشروع")
    estimated_hours = models.DecimalField("الساعات المقدرة", max_digits=8, decimal_places=2, default=0)
    actual_hours = models.DecimalField("الساعات الفعلية", max_digits=8, decimal_places=2, default=0)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "مهمة مشروع"
        verbose_name_plural = "مهام المشاريع"
        ordering = ["project", "due_date", "id"]

    def __str__(self):
        return self.title

    @property
    def effective_progress(self):
        return flt(100) if self.status == TaskStatus.DONE else flt(self.progress)

    @property
    def is_overdue(self):
        from django.utils import timezone

        return self.status in (TaskStatus.OPEN, TaskStatus.WORKING) and self.due_date and \
            self.due_date < timezone.localdate()

    def clean(self):
        if self.progress is not None and not 0 <= self.progress <= 100:
            raise ValidationError({"progress": "بين 0 و100."})

    @property
    def available_actions(self):
        return [] if self.status == TaskStatus.DONE else [("mark_done", "تمت")]

    def mark_done(self, user=None):
        self.status, self.progress = TaskStatus.DONE, 100
        self.save(update_fields=["status", "progress"])
