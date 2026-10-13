"""سندات التبرعات للجهات غير الربحية: قبض التبرع وقيده لإيراد التبرعات وللصندوق المقيد إن وُجد."""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from accounts.ledger import cancel_gl_entries, make_gl_entries
from accounts.models import Account, AccountType, Fund, FundType, RootType
from core.models import NamingSeries, SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


class DonationType(models.TextChoices):
    GENERAL = "general", "تبرع عام (غير مقيد)"
    RESTRICTED = "restricted", "تبرع مقيد بغرض"
    ZAKAT = "zakat", "زكاة"
    GRANT = "grant", "منحة"
    SPONSORSHIP = "sponsorship", "كفالة"
    ENDOWMENT = "endowment", "وقف"
    MEMBERSHIP = "membership", "اشتراك عضوية"


# حساب الإيراد الافتراضي لكل نوع من الدليل المحاسبي الموحد للجهات غير الربحية
INCOME_ACCOUNTS = {"general": "31201001", "restricted": "31102002", "zakat": "31101001", "grant": "31105002",
                   "sponsorship": "31102001", "endowment": "31301003", "membership": "31205001"}
# فرع الإيرادات الذي يجب أن يُقيد فيه كل نوع: 311 مقيدة، 312 غير مقيدة، 313 أوقاف
INCOME_BRANCHES = {"general": "312", "restricted": "311", "zakat": "311", "grant": "311", "sponsorship": "311",
                   "endowment": "313", "membership": "312"}
# نوع الصندوق المطلوب لكل نوع تبرع (None = لا صندوق أو صندوق غير مقيد)
REQUIRED_FUND_TYPE = {"restricted": FundType.RESTRICTED, "zakat": FundType.RESTRICTED, "grant": FundType.RESTRICTED,
                      "sponsorship": FundType.RESTRICTED, "endowment": FundType.ENDOWMENT}


class PaymentMethod(models.TextChoices):
    CASH = "cash", "نقداً"
    TRANSFER = "transfer", "تحويل بنكي"
    CARD = "card", "بطاقة / مدى"
    ONLINE = "online", "دفع إلكتروني"
    CHEQUE = "cheque", "شيك"


class DonorType(models.TextChoices):
    INDIVIDUAL = "individual", "فرد"
    COMPANY = "company", "شركة / مؤسسة"
    ENTITY = "entity", "جهة مانحة"


class Donor(TenantModel):
    """الداعم: متبرع أو كافل، تُربط به سندات التبرعات والكفالات."""

    name = models.CharField("اسم الداعم", max_length=150)
    donor_type = models.CharField("النوع", max_length=12, choices=DonorType.choices, default=DonorType.INDIVIDUAL)
    phone = models.CharField("الجوال", max_length=20, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    city = models.CharField("المدينة", max_length=60, blank=True)
    is_active = models.BooleanField("نشط", default=True)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "داعم"
        verbose_name_plural = "الداعمون"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="donor")]

    def __str__(self):
        return self.name

    def donation_total(self, year=None):
        receipts = self.receipts.filter(docstatus=1)
        if year:
            receipts = receipts.filter(posting_date__year=year)
        return flt(receipts.aggregate(t=models.Sum("amount"))["t"])


class DonationReceipt(SubmittableDocument):
    naming_prefix = "DON"

    donor = models.ForeignKey(Donor, models.PROTECT, null=True, blank=True, related_name="receipts",
                              limit_choices_to={"is_active": True}, verbose_name="الداعم",
                              help_text="اختياري: لربط التبرع بسجل الداعم")
    donor_name = models.CharField("اسم المتبرع", max_length=150, blank=True, help_text="اتركه فارغاً لـ«فاعل خير»")
    donor_phone = models.CharField("جوال المتبرع", max_length=20, blank=True)
    donation_type = models.CharField("نوع التبرع", max_length=12, choices=DonationType.choices,
                                     default=DonationType.GENERAL)
    fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="+",
                             limit_choices_to={"is_active": True}, verbose_name="الصندوق",
                             help_text="إلزامي للتبرع المقيد والزكاة والمنحة والكفالة والوقف")
    amount = models.DecimalField("المبلغ", max_digits=18, decimal_places=2)
    payment_method = models.CharField("طريقة الدفع", max_length=10, choices=PaymentMethod.choices,
                                      default=PaymentMethod.CASH)
    account = models.ForeignKey(Account, models.PROTECT, related_name="+", verbose_name="حساب الصندوق / البنك",
                                limit_choices_to={"is_group": False, "account_type__in": [AccountType.CASH,
                                                                                          AccountType.BANK]})
    income_account = models.ForeignKey(Account, models.PROTECT, null=True, blank=True, related_name="+",
                                       limit_choices_to={"is_group": False, "root_type": RootType.INCOME},
                                       verbose_name="حساب الإيراد", help_text="يُحدد تلقائياً حسب نوع التبرع")
    reference_no = models.CharField("رقم المرجع (حوالة / عملية)", max_length=50, blank=True)
    sponsorship_due = models.ForeignKey("funds.SponsorshipDue", models.PROTECT, null=True, blank=True,
                                        related_name="receipts", editable=False, verbose_name="استحقاق الكفالة")
    membership_fee = models.ForeignKey("governance.MembershipFee", models.PROTECT, null=True, blank=True,
                                       related_name="receipts", editable=False, verbose_name="اشتراك العضوية")

    class Meta(SubmittableDocument.Meta):
        verbose_name = "سند تبرع"
        verbose_name_plural = "سندات التبرعات"

    @property
    def donor_display(self):
        return self.donor_name or (self.donor.name if self.donor_id else "فاعل خير")

    def clean(self):
        if self.donor_id and not self.donor_name:
            self.donor_name, self.donor_phone = self.donor.name, self.donor_phone or self.donor.phone
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "المبلغ يجب أن يكون أكبر من صفر."})
        required = REQUIRED_FUND_TYPE.get(self.donation_type)
        fund = self.fund if self.fund_id else None
        if self.donation_type == "zakat" and fund is None:
            fund = self.fund = Fund.objects.filter(name="صندوق الزكاة", is_active=True).first()
        if required and fund is None:
            raise ValidationError({"fund": f"حدد الصندوق لـ«{self.get_donation_type_display()}»."})
        if required and fund.fund_type != required:
            raise ValidationError({"fund": f"«{self.get_donation_type_display()}» يُقيد في صندوق "
                                           f"{FundType(required).label}، و«{fund}» {fund.get_fund_type_display()}."})
        if not required and fund is not None and fund.fund_type != FundType.UNRESTRICTED:
            raise ValidationError({"fund": "التبرع العام لا يُقيد في صندوق مقيد. اختر «تبرع مقيد بغرض» أو اترك الصندوق."})
        if self.income_account_id and self.company_id:
            from accounts.chart import uses_nonprofit_chart

            branch = INCOME_BRANCHES[self.donation_type]
            if uses_nonprofit_chart(self.company) and not self.income_account.number.startswith(branch):
                raise ValidationError({"income_account": f"«{self.get_donation_type_display()}» يُقيد في حسابات الفرع "
                                                         f"{branch} من الدليل المحاسبي الموحد."})

    def calculate_totals(self):
        if not self.income_account_id:
            self.income_account = Account.objects.filter(number=INCOME_ACCOUNTS[self.donation_type],
                                                         is_group=False).first()

    def validate_for_submit(self):
        self.clean()
        if not self.income_account_id:
            raise ValidationError(f"حساب الإيراد {INCOME_ACCOUNTS[self.donation_type]} غير موجود في دليل الحسابات. "
                                  "حدد حساب الإيراد يدوياً.")

    def on_submit(self):
        remarks = f"{self.get_donation_type_display()} من {self.donor_display}"
        make_gl_entries(self, [{"account": self.account, "debit": self.amount},
                               {"account": self.income_account, "credit": self.amount, "fund": self.fund}],
                        remarks=self.remarks or remarks)

        self._refresh_links()

    def on_cancel(self):
        cancel_gl_entries(self)
        self._refresh_links()

    def _refresh_links(self):
        """تحديث الاستحقاق المرتبط (كفالة أو اشتراك عضوية) بما حُصّل له."""
        for link in ("sponsorship_due", "membership_fee"):
            if getattr(self, f"{link}_id"):
                getattr(self, link).refresh_collected()


# ───────────────────────────── الكفالات ─────────────────────────────

class SponsorshipType(TenantModel):
    """نوع الكفالة: مبلغها الشهري الافتراضي، وبرنامج المساعدات الذي تُصرف منه للمكفول (ومنه حساب المصروف والصندوق)."""

    name = models.CharField("نوع الكفالة", max_length=100)
    monthly_amount = models.DecimalField("المبلغ الشهري الافتراضي", max_digits=12, decimal_places=2, default=0)
    aid_program = models.ForeignKey("beneficiaries.AidProgram", models.PROTECT, related_name="+",
                                    verbose_name="برنامج الصرف للمكفول",
                                    help_text="يحدد حساب مصروف الكفالة وصندوقها (التحصيل يُقيد في نفس الصندوق)")
    categories = models.ManyToManyField("beneficiaries.BeneficiaryCategory", blank=True, related_name="+",
                                        verbose_name="فئات المكفولين", help_text="فارغ = كل الفئات")

    class Meta:
        verbose_name = "نوع كفالة"
        verbose_name_plural = "أنواع الكفالات"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="sponsorshiptype")]

    def __str__(self):
        return self.name


class SponsorshipStatus(models.TextChoices):
    ACTIVE = "active", "سارية"
    PAUSED = "paused", "متوقفة مؤقتاً"
    ENDED = "ended", "منتهية"


class Sponsorship(TenantModel):
    number = models.CharField("رقم الكفالة", max_length=30, editable=False)
    donor = models.ForeignKey(Donor, models.PROTECT, related_name="sponsorships", verbose_name="الكافل")
    beneficiary = models.ForeignKey("beneficiaries.Beneficiary", models.PROTECT, related_name="sponsorships",
                                    verbose_name="المكفول")
    sponsorship_type = models.ForeignKey(SponsorshipType, models.PROTECT, related_name="sponsorships",
                                         verbose_name="نوع الكفالة")
    monthly_amount = models.DecimalField("المبلغ الشهري", max_digits=12, decimal_places=2, default=0,
                                         help_text="صفر = المبلغ الافتراضي لنوع الكفالة")
    start_date = models.DateField("تاريخ البداية", default=timezone.localdate)
    end_date = models.DateField("تاريخ الانتهاء", null=True, blank=True, help_text="فارغ = مستمرة")
    payment_method = models.CharField("طريقة السداد", max_length=10, choices=PaymentMethod.choices,
                                      default=PaymentMethod.TRANSFER)
    status = models.CharField("الحالة", max_length=10, choices=SponsorshipStatus.choices,
                              default=SponsorshipStatus.ACTIVE, editable=False)
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "كفالة"
        verbose_name_plural = "الكفالات"
        ordering = ["-id"]
        constraints = [tenant_unique("number", model_label="sponsorship")]

    def __str__(self):
        return f"{self.number} — {self.beneficiary.full_name}" if self.number else str(self.beneficiary)

    def clean(self):
        from beneficiaries.models import BeneficiaryStatus

        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ الانتهاء يجب أن يكون بعد البداية."})
        if self.beneficiary_id and self.beneficiary.status != BeneficiaryStatus.APPROVED:
            raise ValidationError({"beneficiary": "الكفالة للمستفيدين المعتمدين فقط."})
        if self.sponsorship_type_id and self.beneficiary_id:
            categories = list(self.sponsorship_type.categories.all())
            if categories and self.beneficiary.category not in categories:
                raise ValidationError({"beneficiary": f"فئة المستفيد ليست من فئات «{self.sponsorship_type}»."})
            duplicate = Sponsorship.objects.filter(beneficiary=self.beneficiary, sponsorship_type=self.sponsorship_type,
                                                   status=SponsorshipStatus.ACTIVE).exclude(pk=self.pk).first()
            if duplicate is not None and self.status == SponsorshipStatus.ACTIVE:
                raise ValidationError({"beneficiary": f"المستفيد مكفول بالفعل في «{self.sponsorship_type}» "
                                                      f"لدى {duplicate.donor} ({duplicate.number})."})

    def save(self, *args, **kwargs):
        if not self.monthly_amount and self.sponsorship_type_id:
            self.monthly_amount = self.sponsorship_type.monthly_amount
        if not self.number:
            from core.models import Company

            self.number = NamingSeries.next_name(self.company if self.company_id else Company.current(), "SPN")
        super().save(*args, **kwargs)

    def covers(self, period):
        """هل الكفالة سارية في الشهر الذي يبدأ بـ period؟"""
        month_end = add_months(period, 1) - timezone.timedelta(days=1)
        return (self.status == SponsorshipStatus.ACTIVE and self.start_date <= month_end
                and (self.end_date is None or self.end_date >= period))

    @property
    def outstanding(self):
        return flt(sum((d.outstanding for d in self.dues.exclude(status=DueStatus.WAIVED)), flt(0)))

    @property
    def available_actions(self):
        if self.status == SponsorshipStatus.ACTIVE:
            return [("pause", "إيقاف مؤقت"), ("end", "إنهاء الكفالة")]
        if self.status == SponsorshipStatus.PAUSED:
            return [("resume", "استئناف الكفالة"), ("end", "إنهاء الكفالة")]
        return []

    def pause(self, user):
        self.status = SponsorshipStatus.PAUSED
        self.save(update_fields=["status"])

    def resume(self, user):
        self.status = SponsorshipStatus.ACTIVE
        self.full_clean()
        self.save(update_fields=["status"])

    def end(self, user):
        self.status = SponsorshipStatus.ENDED
        if not self.end_date:
            self.end_date = timezone.localdate()
        self.save(update_fields=["status", "end_date"])


def add_months(date, months):
    month = date.month - 1 + months
    return date.replace(year=date.year + month // 12, month=month % 12 + 1, day=1)


class DueStatus(models.TextChoices):
    PENDING = "pending", "غير محصّل"
    PARTIAL = "partial", "محصّل جزئياً"
    PAID = "paid", "محصّل"
    WAIVED = "waived", "معفى"


class SponsorshipDue(TenantModel):
    """استحقاق شهري على الكافل: يُحصّل بسند تبرع كفالة، ويُصرف للمكفول بمستند صرف مساعدة."""

    sponsorship = models.ForeignKey(Sponsorship, models.CASCADE, related_name="dues", verbose_name="الكفالة")
    period = models.DateField("الشهر")
    amount = models.DecimalField("المبلغ", max_digits=12, decimal_places=2)
    collected_amount = models.DecimalField("المحصّل", max_digits=12, decimal_places=2, default=0, editable=False)
    status = models.CharField("الحالة", max_length=10, choices=DueStatus.choices, default=DueStatus.PENDING,
                              editable=False)
    disbursement = models.ForeignKey("beneficiaries.AidDisbursement", models.SET_NULL, null=True, blank=True,
                                     related_name="+", editable=False, verbose_name="الصرف للمكفول")

    class Meta:
        verbose_name = "استحقاق كفالة"
        verbose_name_plural = "استحقاقات الكفالات"
        ordering = ["-period", "id"]
        constraints = [models.UniqueConstraint(fields=["sponsorship", "period"], name="sponsorship_due_per_month",
                                               violation_error_message="يوجد استحقاق لهذه الكفالة في نفس الشهر.")]

    def __str__(self):
        return f"{self.sponsorship.number} — {self.period:%Y-%m}"

    @property
    def outstanding(self):
        return flt(max(flt(self.amount) - flt(self.collected_amount), 0))

    @property
    def is_overdue(self):
        return self.status in (DueStatus.PENDING, DueStatus.PARTIAL) and self.period < timezone.localdate().replace(day=1)

    def refresh_collected(self):
        total = self.receipts.filter(docstatus=1).aggregate(t=models.Sum("amount"))["t"]
        self.collected_amount = flt(total)
        if self.status != DueStatus.WAIVED:
            self.status = (DueStatus.PAID if self.collected_amount >= self.amount else
                           DueStatus.PARTIAL if self.collected_amount else DueStatus.PENDING)
        self.save(update_fields=["collected_amount", "status"])

    @property
    def available_actions(self):
        actions = []
        if self.status in (DueStatus.PENDING, DueStatus.PARTIAL):
            actions += [("collect", "تسجيل التحصيل"), ("waive", "إعفاء الكافل من هذا الشهر")]
        if self.disbursement_id is None and self.sponsorship.beneficiary.status == "approved":
            actions.append(("pay_beneficiary", "صرف للمكفول"))
        return actions

    def collect(self, user):
        """مسودة سند تبرع كفالة بالمتبقي، في صندوق الكفالة، تُراجع وتُعتمد."""
        sponsorship = self.sponsorship
        program = sponsorship.sponsorship_type.aid_program
        return DonationReceipt.objects.create(
            donor=sponsorship.donor, donor_name=sponsorship.donor.name, donor_phone=sponsorship.donor.phone,
            donation_type=DonationType.SPONSORSHIP, fund=program.fund, amount=self.outstanding,
            payment_method=sponsorship.payment_method, account=self.company.default_cash_account,
            sponsorship_due=self, created_by=user,
            remarks=f"كفالة {sponsorship.beneficiary.full_name} عن {self.period:%Y-%m}")

    def waive(self, user):
        self.status = DueStatus.WAIVED
        self.save(update_fields=["status"])

    def pay_beneficiary(self, user):
        from beneficiaries.models import AidDisbursement

        sponsorship = self.sponsorship
        program = sponsorship.sponsorship_type.aid_program
        doc = AidDisbursement.objects.create(beneficiary=sponsorship.beneficiary, program=program, amount=self.amount,
                                             fund=program.fund, created_by=user,
                                             remarks=f"كفالة {sponsorship.number} عن {self.period:%Y-%m}")
        doc.calculate_totals()
        doc.save()
        self.disbursement = doc
        self.save(update_fields=["disbursement"])
        return doc


def generate_dues(period):
    """استحقاقات الشهر لكل الكفالات السارية في الشركة الحالية (لا تتكرر). يعيد عدد ما أُنشئ."""
    period = period.replace(day=1)
    created = 0
    for sponsorship in Sponsorship.objects.filter(status=SponsorshipStatus.ACTIVE).select_related("sponsorship_type"):
        if sponsorship.covers(period) and not sponsorship.dues.filter(period=period).exists():
            SponsorshipDue.objects.create(sponsorship=sponsorship, period=period, amount=sponsorship.monthly_amount)
            created += 1
    return created
