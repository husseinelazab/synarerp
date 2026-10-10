"""الأصول الثابتة: سجل الأصول، جدول الإهلاك بالقسط الثابت، الترحيل الشهري، والاستبعاد أو البيع."""
import calendar
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from core.models import DocStatus, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt

DEFAULT_CATEGORIES = [
    # (الفئة، رقم حساب الأصل، العمر الإنتاجي بالأشهر)
    ("الأثاث والمفروشات", "1201", 120),
    ("الأجهزة والمعدات", "1202", 60),
    ("السيارات", "1203", 60),
]


def month_end(date):
    return date.replace(day=calendar.monthrange(date.year, date.month)[1])


def add_months(date, months):
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    return datetime.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


class AssetCategory(TenantModel):
    name = models.CharField("الفئة", max_length=100)
    asset_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="حساب الأصل",
        limit_choices_to={"is_group": False, "root_type": "asset"})
    accumulated_depreciation_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="حساب مجمع الإهلاك",
        limit_choices_to={"is_group": False, "root_type": "asset"})
    depreciation_expense_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="حساب مصروف الإهلاك",
        limit_choices_to={"is_group": False, "root_type": "expense"})
    useful_life_months = models.PositiveIntegerField("العمر الإنتاجي الافتراضي (أشهر)", default=60)

    class Meta:
        verbose_name = "فئة أصول"
        verbose_name_plural = "فئات الأصول"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="assetcategory")]

    def __str__(self):
        return self.name


class AssetStatus(models.TextChoices):
    ACTIVE = "active", "قيد الاستخدام"
    FULLY_DEPRECIATED = "fully_depreciated", "مُهلك بالكامل"
    DISPOSED = "disposed", "مُستبعد / مُباع"


class Asset(SubmittableDocument):
    naming_prefix = "AST"

    asset_name = models.CharField("اسم الأصل", max_length=200)
    category = models.ForeignKey(AssetCategory, models.PROTECT, verbose_name="الفئة")
    serial_no = models.CharField("الرقم التسلسلي / اللوحة", max_length=100, blank=True)
    location = models.CharField("الموقع", max_length=150, blank=True)
    custodian = models.ForeignKey("hr.Employee", models.SET_NULL, null=True, blank=True, verbose_name="عهدة الموظف")
    cost_center = models.ForeignKey("accounts.CostCenter", models.PROTECT, null=True, blank=True,
                                    verbose_name="مركز التكلفة", limit_choices_to={"is_group": False})
    purchase_date = models.DateField("تاريخ الشراء")
    gross_amount = models.DecimalField("تكلفة الأصل (بدون الضريبة)", max_digits=18, decimal_places=2)
    salvage_value = models.DecimalField("القيمة التخريدية", max_digits=18, decimal_places=2, default=0,
                                        help_text="القيمة المتوقعة في نهاية العمر الإنتاجي؛ لا تُهلك")
    useful_life_months = models.PositiveIntegerField("العمر الإنتاجي المتبقي (أشهر)", null=True, blank=True,
                                                     help_text="يُترك فارغاً لاستخدام العمر الافتراضي للفئة")
    depreciation_start_date = models.DateField("بداية الإهلاك", null=True, blank=True,
                                               help_text="يُترك فارغاً ليبدأ من تاريخ الشراء")
    opening_accumulated_depreciation = models.DecimalField(
        "إهلاك متراكم سابق", max_digits=18, decimal_places=2, default=0,
        help_text="للأصول المملوكة قبل استخدام النظام: ما أُهلك منها سابقاً (يُقيد مقابل الأرصدة الافتتاحية)")
    credit_account = models.ForeignKey(
        "accounts.Account", models.PROTECT, related_name="+", verbose_name="الحساب المقابل",
        limit_choices_to={"is_group": False},
        help_text="البنك أو الصندوق إن دُفع ثمنه، أو الموردون (مع المورد) إن كان آجلاً، أو الأرصدة الافتتاحية لأصل مملوك سابقاً")
    supplier = models.ForeignKey("buying.Supplier", models.PROTECT, null=True, blank=True, verbose_name="المورد")

    status = models.CharField("حالة الأصل", max_length=20, choices=AssetStatus.choices, default=AssetStatus.ACTIVE,
                              editable=False)
    accumulated_depreciation = models.DecimalField("مجمع الإهلاك", max_digits=18, decimal_places=2, default=0,
                                                   editable=False)
    disposal_date = models.DateField("تاريخ الاستبعاد", null=True, blank=True, editable=False)
    disposal_entry = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True,
                                       related_name="+", editable=False, verbose_name="قيد الاستبعاد")

    class Meta(SubmittableDocument.Meta):
        verbose_name = "أصل ثابت"
        verbose_name_plural = "الأصول الثابتة"

    def __str__(self):
        return f"{self.name} - {self.asset_name}" if self.name else self.asset_name

    @property
    def book_value(self):
        return flt(self.gross_amount - self.accumulated_depreciation)

    @property
    def life_months(self):
        return self.useful_life_months or (self.category.useful_life_months if self.category_id else 0)

    @property
    def start_date(self):
        return self.depreciation_start_date or self.purchase_date

    @property
    def depreciable_amount(self):
        return flt(self.gross_amount - self.salvage_value - self.opening_accumulated_depreciation)

    def clean(self):
        if self.gross_amount is not None and self.gross_amount <= 0:
            raise ValidationError({"gross_amount": "تكلفة الأصل يجب أن تكون أكبر من صفر."})
        if self.gross_amount and self.salvage_value is not None and self.opening_accumulated_depreciation is not None \
                and self.salvage_value + self.opening_accumulated_depreciation > self.gross_amount:
            raise ValidationError("القيمة التخريدية مع الإهلاك السابق لا تتجاوز تكلفة الأصل.")
        # الإهلاك شهري، فيكفي ألا يسبق شهرُ البداية شهرَ الشراء
        if self.depreciation_start_date and self.purchase_date and \
                self.depreciation_start_date.replace(day=1) < self.purchase_date.replace(day=1):
            raise ValidationError({"depreciation_start_date": "بداية الإهلاك لا تسبق شهر الشراء."})

    def validate_for_submit(self):
        from accounts.models import AccountType

        self.clean()
        if not self.life_months:
            raise ValidationError("حدد العمر الإنتاجي للأصل أو لفئته.")
        if self.credit_account.account_type == AccountType.PAYABLE and not self.supplier_id:
            raise ValidationError("الحساب المقابل حساب موردين؛ حدد المورد.")
        if self.opening_accumulated_depreciation:
            self.company.require_account("default_opening_account")

    def on_submit(self):
        from accounts.ledger import make_gl_entries

        category = self.category
        entries = [
            {"account": category.asset_account, "debit": self.gross_amount, "cost_center": self.cost_center},
            {"account": self.credit_account, "credit": self.gross_amount, "supplier": self.supplier},
        ]
        if self.opening_accumulated_depreciation:
            entries += [
                {"account": self.company.default_opening_account, "debit": self.opening_accumulated_depreciation,
                 "remarks": "إهلاك متراكم سابق"},
                {"account": category.accumulated_depreciation_account,
                 "credit": self.opening_accumulated_depreciation, "remarks": "إهلاك متراكم سابق"},
            ]
        make_gl_entries(self, entries, remarks=f"إثبات الأصل {self.asset_name}")
        self.accumulated_depreciation = self.opening_accumulated_depreciation
        self.save(update_fields=["accumulated_depreciation"])
        self.build_schedule()

    def build_schedule(self):
        """قسط ثابت شهري من شهر بداية الإهلاك؛ آخر قسط يستوعب فرق التقريب."""
        self.schedule.all().delete()
        months, amount = self.life_months, self.depreciable_amount
        if amount <= 0:
            self.refresh_status()
            return
        monthly = flt(amount / months)
        rows, accumulated = [], flt(self.opening_accumulated_depreciation)
        first = month_end(self.start_date)
        for i in range(months):
            value = monthly if i < months - 1 else flt(amount - monthly * (months - 1))
            accumulated += value
            rows.append(AssetDepreciation(asset=self, schedule_date=month_end(add_months(first, i)), amount=value,
                                          accumulated_after=accumulated))
        AssetDepreciation.objects.bulk_create(rows)

    def before_cancel(self):
        if self.schedule.filter(journal_entry__isnull=False).exists():
            raise ValidationError("رُحّل إهلاك لهذا الأصل؛ ألغِ قيود الإهلاك أولاً.")
        if self.status == AssetStatus.DISPOSED:
            raise ValidationError("الأصل مُستبعد؛ ألغِ قيد الاستبعاد أولاً.")

    def on_cancel(self):
        from accounts.ledger import cancel_gl_entries

        cancel_gl_entries(self)
        self.schedule.all().delete()

    def refresh_status(self):
        posted = self.schedule.filter(journal_entry__isnull=False).aggregate(t=models.Sum("amount"))["t"] or 0
        self.accumulated_depreciation = flt(self.opening_accumulated_depreciation + posted)
        if self.status != AssetStatus.DISPOSED:
            pending = self.schedule.filter(journal_entry__isnull=True).exists()
            self.status = AssetStatus.ACTIVE if pending else AssetStatus.FULLY_DEPRECIATED
        self.save(update_fields=["accumulated_depreciation", "status"])


class AssetDepreciation(models.Model):
    asset = models.ForeignKey(Asset, models.CASCADE, related_name="schedule")
    schedule_date = models.DateField("التاريخ")
    amount = models.DecimalField("قسط الإهلاك", max_digits=18, decimal_places=2)
    accumulated_after = models.DecimalField("المجمع بعد القسط", max_digits=18, decimal_places=2)
    journal_entry = models.ForeignKey("accounts.JournalEntry", models.SET_NULL, null=True, blank=True,
                                      related_name="+", verbose_name="قيد الإهلاك")

    class Meta:
        verbose_name = "قسط إهلاك"
        verbose_name_plural = "جدول الإهلاك"
        ordering = ["schedule_date", "id"]
