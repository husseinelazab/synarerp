"""سندات التبرعات للجهات غير الربحية: قبض التبرع وقيده لإيراد التبرعات وللصندوق المقيد إن وُجد."""
from django.core.exceptions import ValidationError
from django.db import models

from accounts.ledger import cancel_gl_entries, make_gl_entries
from accounts.models import Account, AccountType, Fund, FundType, RootType
from core.models import SubmittableDocument


class DonationType(models.TextChoices):
    GENERAL = "general", "تبرع عام (غير مقيد)"
    RESTRICTED = "restricted", "تبرع مقيد بغرض"
    ZAKAT = "zakat", "زكاة"
    GRANT = "grant", "منحة"
    SPONSORSHIP = "sponsorship", "كفالة"
    ENDOWMENT = "endowment", "وقف"


# حساب الإيراد الافتراضي لكل نوع (من دليل حسابات الجمعيات)
INCOME_ACCOUNTS = {"general": "4201", "restricted": "4202", "zakat": "4203", "grant": "4204", "sponsorship": "4207",
                   "endowment": "4209"}
# نوع الصندوق المطلوب لكل نوع تبرع (None = لا صندوق أو صندوق غير مقيد)
REQUIRED_FUND_TYPE = {"restricted": FundType.RESTRICTED, "zakat": FundType.RESTRICTED, "grant": FundType.RESTRICTED,
                      "sponsorship": FundType.RESTRICTED, "endowment": FundType.ENDOWMENT}


class PaymentMethod(models.TextChoices):
    CASH = "cash", "نقداً"
    TRANSFER = "transfer", "تحويل بنكي"
    CARD = "card", "بطاقة / مدى"
    ONLINE = "online", "دفع إلكتروني"
    CHEQUE = "cheque", "شيك"


class DonationReceipt(SubmittableDocument):
    naming_prefix = "DON"

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

    class Meta(SubmittableDocument.Meta):
        verbose_name = "سند تبرع"
        verbose_name_plural = "سندات التبرعات"

    @property
    def donor(self):
        return self.donor_name or "فاعل خير"

    def clean(self):
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
        remarks = f"{self.get_donation_type_display()} من {self.donor}"
        make_gl_entries(self, [{"account": self.account, "debit": self.amount},
                               {"account": self.income_account, "credit": self.amount, "fund": self.fund}],
                        remarks=self.remarks or remarks)

    def on_cancel(self):
        cancel_gl_entries(self)
