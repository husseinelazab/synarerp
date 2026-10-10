import calendar
import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from saas.models import Plan, Subscription, SubscriptionPayment, SubscriptionStatus

DEFAULT_PLANS = [
    {"code": "basic", "name": "الأساسية", "description": "للمنشآت الصغيرة: محاسبة، مبيعات، مشتريات، مخزون",
     "price_monthly": 149, "price_yearly": 1490, "max_users": 3, "includes_pos": False, "sort_order": 1},
    {"code": "pro", "name": "الاحترافية", "description": "كل ما في الأساسية مع نقاط البيع وعدد أكبر من المستخدمين",
     "price_monthly": 299, "price_yearly": 2990, "max_users": 10, "includes_pos": True, "sort_order": 2},
    {"code": "enterprise", "name": "المؤسسات", "description": "للشركات المتوسطة والفروع المتعددة",
     "price_monthly": 599, "price_yearly": 5990, "max_users": 50, "includes_pos": True, "sort_order": 3},
]


def ensure_default_plans():
    for data in DEFAULT_PLANS:
        Plan.objects.get_or_create(code=data["code"], defaults=data)


def default_trial_plan():
    ensure_default_plans()
    return Plan.objects.get(code="pro")


def add_months(date, months):
    month_index = date.month - 1 + months
    year, month = date.year + month_index // 12, month_index % 12 + 1
    return datetime.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


def start_subscription(company, plan=None):
    plan = plan or default_trial_plan()
    return Subscription.objects.create(
        company=company, plan=plan, status=SubscriptionStatus.TRIAL,
        period_end=timezone.localdate() + datetime.timedelta(days=plan.trial_days),
    )


@transaction.atomic
def record_payment(subscription, plan, months, amount, method="bank", reference="", user=None):
    """تسجيل دفعة اشتراك وتمديد الصلاحية من نهاية الفترة الحالية (أو من اليوم إن كانت منتهية)."""
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
    return company.memberships.filter(is_active=True).count()


def check_user_limit(company):
    subscription = getattr(company, "subscription", None)
    if subscription and active_user_count(company) >= subscription.plan.max_users:
        raise ValidationError(
            f"وصلت للحد الأقصى لعدد المستخدمين في باقتك ({subscription.plan.max_users}). يرجى ترقية الباقة."
        )
