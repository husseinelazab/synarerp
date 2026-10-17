import calendar
import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from saas.models import Plan, Subscription, SubscriptionPayment, SubscriptionStatus

DEFAULT_PLANS = [
    {"code": "basic", "name": "الأساسية", "description": "للمنشآت الصغيرة: محاسبة، مبيعات، مشتريات، مخزون",
     "price_monthly": 149, "price_yearly": 1490, "max_users": 3, "includes_pos": False, "includes_ai": False, "sort_order": 1},
    {"code": "pro", "name": "الاحترافية", "description": "كل ما في الأساسية مع نقاط البيع وقراءة الفواتير بالذكاء الاصطناعي وعدد أكبر من المستخدمين",
     "price_monthly": 299, "price_yearly": 2990, "max_users": 10, "includes_pos": True, "includes_ai": True, "sort_order": 2},
    {"code": "enterprise", "name": "المؤسسات", "description": "للشركات المتوسطة والفروع المتعددة",
     "price_monthly": 599, "price_yearly": 5990, "max_users": 50, "includes_pos": True, "includes_ai": True, "sort_order": 3},
]


def ensure_default_plans():
    from core.models import OrganizationType
    for data in DEFAULT_PLANS:
        Plan.objects.get_or_create(code=data["code"], defaults=data)
        nonprofit = dict(data, code="nonprofit-" + data["code"], name="الجمعيات — " + data["name"],
                         description="للجمعيات: المحاسبة والحوكمة والمستفيدون والبرامج المتخصصة",
                         organization_type=OrganizationType.NONPROFIT, includes_pos=False)
        Plan.objects.get_or_create(code=nonprofit["code"], defaults=nonprofit)


def default_trial_plan(company=None):
    from core.models import OrganizationType
    ensure_default_plans()
    kind = company.organization_type if company else OrganizationType.BUSINESS
    plans = Plan.objects.filter(is_active=True, organization_type=kind)
    plan = plans.filter(code="nonprofit-pro" if kind == OrganizationType.NONPROFIT else "pro").first() or plans.first()
    if plan is None:
        raise ValidationError("لا توجد باقة نشطة لهذا النوع من المنشآت. تواصل مع إدارة المنصة.")
    return plan


def add_months(date, months):
    month_index = date.month - 1 + months
    year, month = date.year + month_index // 12, month_index % 12 + 1
    return datetime.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


def start_subscription(company, plan=None):
    plan = plan or default_trial_plan(company)
    if not plan.is_active or plan.organization_type != company.organization_type:
        raise ValidationError("الباقة غير نشطة أو لا تخص نوع المنشأة.")
    return Subscription.objects.create(
        company=company, plan=plan, status=SubscriptionStatus.TRIAL,
        period_end=timezone.localdate() + datetime.timedelta(days=plan.trial_days),
    )


@transaction.atomic
def record_payment(subscription, plan, months, amount, method="bank", reference="", user=None):
    """تسجيل دفعة اشتراك وتمديد الصلاحية من نهاية الفترة الحالية (أو من اليوم إن كانت منتهية)."""
    if not plan.is_active or plan.organization_type != subscription.company.organization_type:
        raise ValidationError("اختر باقة نشطة تخص نوع المنشأة.")
    if months <= 0:
        raise ValidationError("عدد الأشهر يجب أن يكون أكبر من صفر.")
    today = timezone.localdate()
    start = subscription.period_end + datetime.timedelta(days=1) if subscription.is_valid and subscription.status == SubscriptionStatus.ACTIVE else today
    end = add_months(start, months) - datetime.timedelta(days=1)
    payment = SubscriptionPayment.objects.create(
        subscription=subscription, plan=plan, months=months, amount=amount, method=method,
        reference=reference, period_start=start, period_end=end, recorded_by=user,
    )
    subscription.plan, subscription.period_end, subscription.status = plan, end, SubscriptionStatus.ACTIVE
    subscription.save()
    return payment


def active_user_count(company):
    # حسابات بوابة الموظفين (الخدمة الذاتية) لا تُحسب ضمن مستخدمي الباقة
    return company.memberships.filter(is_active=True).exclude(role="employee").count()


def check_user_limit(company):
    subscription = getattr(company, "subscription", None)
    if subscription and active_user_count(company) >= subscription.plan.max_users:
        raise ValidationError(
            f"وصلت للحد الأقصى لعدد المستخدمين في باقتك ({subscription.plan.max_users}). يرجى ترقية الباقة."
        )
