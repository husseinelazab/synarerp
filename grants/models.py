"""إدارة المنح: طلبات المنح المقدمة للجهات المانحة، والمنح المعتمدة وموازناتها ودفعاتها وتقارير المانح.

الجهة المانحة سجل «داعم» (funds.Donor) لتظهر سندات استلام الدفعات في كشف حسابها. المنحة تُقيد في صندوق مقيد،
واستلام الدفعة سند تبرع من نوع «منحة» معتمد في صندوق المنحة (هو الذي يُنشئ القيد)، والصرف يُقيد على حسابات
المصروفات ببعد الصندوق فتُقارن الموازنة بالفعلي من دفتر الأستاذ.
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from accounts.funds import CLOSING_VOUCHER
from accounts.models import Account, AccountType, Fund, FundType, GLEntry, RootType
from core.models import TenantModel
from core.tenancy import tenant_unique
from core.utils import flt


def _require_submit_permission(user, company):
    """إجراء يعتمد سنداً محاسبياً: يتطلب صلاحية الاعتماد."""
    from core.models import Membership

    membership = Membership.objects.filter(user=user, company=company).first() if user is not None else None
    if membership is not None and not membership.may_submit:
        raise ValidationError("صلاحيتك لا تشمل اعتماد السندات؛ يسجل الاستلام مستخدم لديه صلاحية الاعتماد.")


def _one_year_after(date):
    """نهاية منحة مدتها سنة من تاريخ البداية."""
    try:
        return date.replace(year=date.year + 1) - timezone.timedelta(days=1)
    except ValueError:  # 29 فبراير
        return date.replace(year=date.year + 1, day=28)


# ───────────────────────────── طلبات المنح ─────────────────────────────

class ApplicationStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    SUBMITTED = "submitted", "مقدم للمانح"
    UNDER_REVIEW = "under_review", "قيد الدراسة"
    APPROVED = "approved", "معتمد"
    REJECTED = "rejected", "مرفوض"


class GrantApplication(TenantModel):
    grantor = models.ForeignKey("funds.Donor", models.PROTECT, related_name="grant_applications",
                                verbose_name="الجهة المانحة", limit_choices_to={"is_active": True},
                                help_text="سجل الداعم (جهة مانحة) لتظهر الدفعات في كشف حسابه")
    title = models.CharField("عنوان المشروع", max_length=200)
    requested_amount = models.DecimalField("المبلغ المطلوب", max_digits=18, decimal_places=2)
    submitted_date = models.DateField("تاريخ التقديم", null=True, blank=True)
    status = models.CharField("الحالة", max_length=14, choices=ApplicationStatus.choices,
                              default=ApplicationStatus.DRAFT, editable=False)
    decision_date = models.DateField("تاريخ القرار", null=True, blank=True, editable=False)
    project = models.ForeignKey("projects.Project", models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="المشروع")
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "طلب منحة"
        verbose_name_plural = "طلبات المنح"
        ordering = ["-id"]

    def __str__(self):
        return f"{self.title} — {self.grantor}" if self.grantor_id else self.title

    def clean(self):
        if self.requested_amount is not None and self.requested_amount <= 0:
            raise ValidationError({"requested_amount": "المبلغ المطلوب يجب أن يكون أكبر من صفر."})

    @property
    def grant(self):
        return Grant.objects.filter(application=self).first()

    @property
    def available_actions(self):
        status = self.status
        if status == ApplicationStatus.DRAFT:
            return [("send", "تقديم الطلب للمانح")]
        if status == ApplicationStatus.SUBMITTED:
            return [("review", "قيد الدراسة"), ("approve", "اعتماد الطلب"), ("decline", "رفض الطلب")]
        if status == ApplicationStatus.UNDER_REVIEW:
            return [("approve", "اعتماد الطلب"), ("decline", "رفض الطلب")]
        if status == ApplicationStatus.APPROVED and self.grant is None:
            return [("create_grant", "إنشاء منحة")]
        return []

    def _set(self, status, **fields):
        self.status = status
        for key, value in fields.items():
            setattr(self, key, value)
        self.save(update_fields=["status", *fields])

    def send(self, user):
        self._set(ApplicationStatus.SUBMITTED, submitted_date=self.submitted_date or timezone.localdate())

    def review(self, user):
        self._set(ApplicationStatus.UNDER_REVIEW)

    def approve(self, user):
        self._set(ApplicationStatus.APPROVED, decision_date=timezone.localdate())

    def decline(self, user):
        self._set(ApplicationStatus.REJECTED, decision_date=timezone.localdate())

    def create_grant(self, user):
        if self.status != ApplicationStatus.APPROVED:
            raise ValidationError("تُنشأ المنحة من طلب معتمد فقط.")
        if self.grant is not None:
            raise ValidationError("أُنشئت منحة لهذا الطلب بالفعل.")
        start = self.decision_date or timezone.localdate()
        return Grant.objects.create(
            application=self, grantor=self.grantor, title=self.title, approved_amount=self.requested_amount,
            start_date=start, end_date=_one_year_after(start), project=self.project, company=self.company)


# ───────────────────────────── المنح المعتمدة ─────────────────────────────

class GrantStatus(models.TextChoices):
    ACTIVE = "active", "سارية"
    SUSPENDED = "suspended", "موقوفة"
    CLOSED = "closed", "مغلقة"


class Grant(TenantModel):
    application = models.OneToOneField(GrantApplication, models.SET_NULL, null=True, blank=True, editable=False,
                                       related_name="+", verbose_name="طلب المنحة")
    grantor = models.ForeignKey("funds.Donor", models.PROTECT, related_name="grants", verbose_name="الجهة المانحة",
                                limit_choices_to={"is_active": True})
    title = models.CharField("اسم المنحة / المشروع", max_length=200)
    agreement_number = models.CharField("رقم اتفاقية المنحة", max_length=60, blank=True)
    approved_amount = models.DecimalField("المبلغ المعتمد", max_digits=18, decimal_places=2)
    start_date = models.DateField("بداية المنحة")
    end_date = models.DateField("نهاية المنحة")
    fund = models.ForeignKey(Fund, models.PROTECT, null=True, blank=True, related_name="grants",
                             limit_choices_to={"is_active": True, "fund_type": FundType.RESTRICTED},
                             verbose_name="الصندوق المقيد",
                             help_text="صندوق مقيد تُقيد فيه دفعات المنحة ومصروفاتها. اتركه فارغاً ثم استخدم "
                                       "«إنشاء صندوق مقيد للمنحة»")
    project = models.ForeignKey("projects.Project", models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="المشروع")
    income_account = models.ForeignKey(Account, models.PROTECT, null=True, blank=True, related_name="+",
                                       limit_choices_to={"is_group": False, "root_type": RootType.INCOME},
                                       verbose_name="حساب إيراد المنحة",
                                       help_text="من فرع الإيرادات المقيدة (311). فارغ = الحساب الافتراضي للمنح")
    status = models.CharField("الحالة", max_length=10, choices=GrantStatus.choices, default=GrantStatus.ACTIVE,
                              editable=False)
    notes = models.TextField("شروط المانح وملاحظات", blank=True)

    class Meta:
        verbose_name = "منحة"
        verbose_name_plural = "المنح المعتمدة"
        ordering = ["-start_date", "-id"]
        constraints = [tenant_unique("title", model_label="grant")]

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "نهاية المنحة يجب أن تكون بعد بدايتها."})
        if self.approved_amount is not None and self.approved_amount <= 0:
            raise ValidationError({"approved_amount": "المبلغ المعتمد يجب أن يكون أكبر من صفر."})
        if self.fund_id:
            if self.fund.fund_type != FundType.RESTRICTED:
                raise ValidationError({"fund": f"المنحة تُقيد في صندوق مقيد، و«{self.fund}» "
                                               f"{self.fund.get_fund_type_display()}."})
            other = Grant.objects.filter(fund_id=self.fund_id).exclude(pk=self.pk).first()
            if other is not None:
                raise ValidationError({"fund": f"الصندوق «{self.fund}» مخصص للمنحة «{other}»؛ لكل منحة صندوقها."})

    # ── الإجراءات ──
    @property
    def available_actions(self):
        actions = []
        if self.fund_id is None:
            actions.append(("create_fund", "إنشاء صندوق مقيد للمنحة"))
        if self.status == GrantStatus.ACTIVE:
            actions += [("suspend", "إيقاف المنحة"), ("close", "إغلاق المنحة")]
        elif self.status == GrantStatus.SUSPENDED:
            actions += [("resume", "استئناف المنحة"), ("close", "إغلاق المنحة")]
        return actions

    @transaction.atomic
    def create_fund(self, user=None):
        if self.fund_id:
            raise ValidationError("للمنحة صندوق بالفعل.")
        name = f"منحة {self.title}"
        if Fund.objects.filter(name=name).exists():
            name = f"{name} ({self.agreement_number or self.pk})"
        self.fund = Fund.objects.create(
            company=self.company, name=name[:150], fund_type=FundType.RESTRICTED, donor=self.grantor.name[:150],
            purpose=f"منحة «{self.title}» من {self.grantor}" + (f" — اتفاقية {self.agreement_number}"
                                                              if self.agreement_number else ""),
            start_date=self.start_date, end_date=self.end_date, target_amount=self.approved_amount)
        self.save(update_fields=["fund"])
        return None

    def suspend(self, user):
        self.status = GrantStatus.SUSPENDED
        self.save(update_fields=["status"])

    def resume(self, user):
        self.status = GrantStatus.ACTIVE
        self.save(update_fields=["status"])

    def close(self, user):
        self.status = GrantStatus.CLOSED
        self.save(update_fields=["status"])

    # ── الأرقام ──
    @property
    def budget_total(self):
        return flt(sum((line.amount for line in self.budget_lines.all()), flt(0)))

    @property
    def received_amount(self):
        """ما استُلم من المانح: سندات المنحة المعتمدة المرتبطة بالدفعات."""
        return flt(sum((i.received_amount for i in self.installments.all()), flt(0)))

    def _expense_entries(self):
        return (GLEntry.objects.filter(fund_id=self.fund_id, is_cancelled=False, account__root_type=RootType.EXPENSE,
                                       posting_date__gte=self.start_date, posting_date__lte=self.end_date)
                .exclude(voucher_type=CLOSING_VOUCHER))

    @property
    def spent_amount(self):
        if not self.fund_id:
            return flt(0)
        sums = self._expense_entries().aggregate(d=models.Sum("debit"), c=models.Sum("credit"))
        return flt(flt(sums["d"]) - flt(sums["c"]))

    @property
    def fund_balance(self):
        return self.fund.balance if self.fund_id else flt(0)

    @property
    def burn_rate(self):
        """نسبة المصروف من المبلغ المعتمد."""
        return flt(self.spent_amount / self.approved_amount * 100) if self.approved_amount else flt(0)

    @property
    def time_elapsed(self):
        """نسبة ما انقضى من مدة المنحة، لمقارنتها بنسبة الصرف."""
        today = timezone.localdate()
        total = (self.end_date - self.start_date).days or 1
        done = min(max((today - self.start_date).days, 0), total)
        return flt(done * 100 / total)

    def budget_vs_actual(self):
        """الموازنة مقابل الفعلي: لكل بند مصروف الصندوق على حسابه خلال مدة المنحة (صافي المدين)."""
        actual_by_account = {}
        if self.fund_id:
            for row in self._expense_entries().values("account").annotate(d=models.Sum("debit"),
                                                                          c=models.Sum("credit")):
                actual_by_account[row["account"]] = flt(flt(row["d"]) - flt(row["c"]))
        rows, used = [], set()
        for line in self.budget_lines.select_related("account"):
            # البنود المتعددة على نفس الحساب: يُنسب الفعلي لأول بند فقط حتى لا يتكرر
            actual = actual_by_account.get(line.account_id, flt(0)) if line.account_id not in used else flt(0)
            used.add(line.account_id)
            rows.append(_variance_row(line.description, line.account, flt(line.amount), actual))
        unbudgeted = [(acc, amt) for acc, amt in actual_by_account.items() if acc not in used and amt]
        if unbudgeted:
            accounts = {a.pk: a for a in Account.objects.filter(pk__in=[a for a, _ in unbudgeted])}
            for acc_id, amt in unbudgeted:
                rows.append({**_variance_row("مصروف غير مدرج في الموازنة", accounts[acc_id], flt(0), amt),
                             "unbudgeted": True})
        budget = flt(sum((r["budget"] for r in rows), flt(0)))
        actual = flt(sum((r["actual"] for r in rows), flt(0)))
        totals = _variance_row("الإجمالي", None, budget, actual)
        return {"rows": rows, "totals": totals}

    def financial_summary(self):
        received = self.received_amount
        fund_totals = self.fund.totals() if self.fund_id else {"received": flt(0), "spent": flt(0),
                                                                "balance": flt(0)}
        return {"approved": flt(self.approved_amount), "received": received,
                "not_received": flt(flt(self.approved_amount) - received),
                "received_percent": flt(received / self.approved_amount * 100) if self.approved_amount else flt(0),
                "spent": self.spent_amount, "fund_received": fund_totals["received"],
                "fund_spent": fund_totals["spent"], "fund_balance": fund_totals["balance"]}


def _variance_row(label, account, budget, actual):
    return {"label": label, "account": account, "budget": budget, "actual": actual,
            "variance": flt(budget - actual), "percent": flt(actual / budget * 100) if budget else None,
            "over": actual > budget}


class GrantBudgetLine(models.Model):
    grant = models.ForeignKey(Grant, models.CASCADE, related_name="budget_lines")
    description = models.CharField("البند", max_length=200)
    account = models.ForeignKey(Account, models.PROTECT, related_name="+", verbose_name="حساب المصروف",
                                limit_choices_to={"is_group": False, "root_type": RootType.EXPENSE})
    amount = models.DecimalField("المبلغ المعتمد للبند", max_digits=18, decimal_places=2)

    class Meta:
        verbose_name = "بند موازنة منحة"
        verbose_name_plural = "موازنة المنحة"
        ordering = ["id"]

    def __str__(self):
        return self.description

    def clean(self):
        if self.amount is not None and self.amount < 0:
            raise ValidationError({"amount": "مبلغ البند لا يكون سالباً."})
        if self.account_id and self.account.root_type != RootType.EXPENSE:
            raise ValidationError({"account": "اختر حساب مصروف."})


# ───────────────────────────── دفعات المنحة ─────────────────────────────

class InstallmentStatus(models.TextChoices):
    PENDING = "pending", "لم تُستلم"
    RECEIVED = "received", "مستلمة"


class GrantInstallment(TenantModel):
    grant = models.ForeignKey(Grant, models.CASCADE, related_name="installments", verbose_name="المنحة")
    due_date = models.DateField("تاريخ الاستحقاق المتوقع")
    amount = models.DecimalField("مبلغ الدفعة", max_digits=18, decimal_places=2)
    account = models.ForeignKey(Account, models.PROTECT, null=True, blank=True, related_name="+",
                                limit_choices_to={"is_group": False,
                                                  "account_type__in": [AccountType.CASH, AccountType.BANK]},
                                verbose_name="حساب البنك المستلم", help_text="فارغ = الصندوق النقدي الافتراضي")
    receipt = models.ForeignKey("funds.DonationReceipt", models.SET_NULL, null=True, blank=True, related_name="+",
                                editable=False, verbose_name="سند الاستلام")
    notes = models.CharField("ملاحظات", max_length=255, blank=True)

    class Meta:
        verbose_name = "دفعة منحة"
        verbose_name_plural = "دفعات المنح"
        ordering = ["due_date", "id"]

    def __str__(self):
        return f"{self.grant} — {self.due_date:%Y-%m-%d}" if self.grant_id and self.due_date else "دفعة منحة"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "مبلغ الدفعة يجب أن يكون أكبر من صفر."})

    @property
    def status(self):
        return InstallmentStatus.RECEIVED if self.received_amount else InstallmentStatus.PENDING

    def get_status_display(self):
        return InstallmentStatus(self.status).label

    @property
    def received_amount(self):
        receipt = self.receipt if self.receipt_id else None
        return flt(receipt.amount) if receipt is not None and receipt.is_submitted else flt(0)

    @property
    def is_overdue(self):
        return self.status == InstallmentStatus.PENDING and self.due_date < timezone.localdate()

    @property
    def available_actions(self):
        if self.status == InstallmentStatus.PENDING:
            return [("receive", "تسجيل الاستلام")]
        return []

    @transaction.atomic
    def receive(self, user=None, posting_date=None):
        """سند تبرع «منحة» معتمد في صندوق المنحة باسم المانح؛ السند هو الذي يُرحّل القيد."""
        from funds.models import DonationReceipt, DonationType, PaymentMethod

        if self.status == InstallmentStatus.RECEIVED:
            raise ValidationError("سُجل استلام هذه الدفعة بالفعل.")
        grant = self.grant
        if grant.status != GrantStatus.ACTIVE:
            raise ValidationError("المنحة غير سارية؛ استأنفها قبل تسجيل الاستلام.")
        if not grant.fund_id:
            raise ValidationError("حدد صندوق المنحة المقيد أولاً (أو أنشئه من إجراءات المنحة).")
        _require_submit_permission(user, self.company)
        account = self.account if self.account_id else self.company.default_cash_account
        if account is None:
            raise ValidationError("حدد حساب البنك المستلم للدفعة.")
        donor = grant.grantor
        receipt = DonationReceipt(
            company=self.company, donor=donor, donor_name=donor.name, donor_phone=donor.phone,
            donation_type=DonationType.GRANT, fund=grant.fund, amount=self.amount,
            income_account=grant.income_account if grant.income_account_id else None,
            payment_method=PaymentMethod.TRANSFER, account=account, created_by=user,
            posting_date=posting_date or timezone.localdate(),
            reference_no=grant.agreement_number[:50],
            remarks=f"دفعة منحة «{grant.title}» من {donor.name}"
                    + (f" — اتفاقية {grant.agreement_number}" if grant.agreement_number else ""))
        receipt.full_clean()
        receipt.calculate_totals()
        receipt.save()
        receipt.submit()
        self.receipt = receipt
        self.save(update_fields=["receipt"])
        return None


# ───────────────────────────── تقارير المانح ─────────────────────────────

class ReportKind(models.TextChoices):
    NARRATIVE = "narrative", "تقرير فني (سردي)"
    FINANCIAL = "financial", "تقرير مالي"


class ReportStatus(models.TextChoices):
    PENDING = "pending", "لم يُسلّم"
    SUBMITTED = "submitted", "سُلّم للمانح"


class GrantReport(TenantModel):
    grant = models.ForeignKey(Grant, models.CASCADE, related_name="reports", verbose_name="المنحة")
    kind = models.CharField("نوع التقرير", max_length=10, choices=ReportKind.choices, default=ReportKind.FINANCIAL)
    period_start = models.DateField("الفترة من", null=True, blank=True)
    period_end = models.DateField("الفترة إلى", null=True, blank=True)
    due_date = models.DateField("موعد التسليم")
    status = models.CharField("الحالة", max_length=10, choices=ReportStatus.choices, default=ReportStatus.PENDING,
                              editable=False)
    submitted_date = models.DateField("تاريخ التسليم", null=True, blank=True, editable=False)
    notes = models.TextField("ملاحظات", blank=True, help_text="أرفق التقرير المسلّم من «المرفقات» في صفحة التقرير")

    class Meta:
        verbose_name = "تقرير للمانح"
        verbose_name_plural = "تقارير المانحين"
        ordering = ["due_date", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.grant}" if self.grant_id else self.get_kind_display()

    def clean(self):
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValidationError({"period_end": "نهاية الفترة يجب أن تكون بعد بدايتها."})

    @property
    def is_overdue(self):
        return self.status == ReportStatus.PENDING and self.due_date < timezone.localdate()

    @property
    def available_actions(self):
        if self.status == ReportStatus.PENDING:
            return [("mark_submitted", "تسجيل تسليم التقرير للمانح")]
        return [("reopen", "إعادة فتح التقرير")]

    def mark_submitted(self, user):
        self.status, self.submitted_date = ReportStatus.SUBMITTED, timezone.localdate()
        self.save(update_fields=["status", "submitted_date"])

    def reopen(self, user):
        self.status, self.submitted_date = ReportStatus.PENDING, None
        self.save(update_fields=["status", "submitted_date"])
