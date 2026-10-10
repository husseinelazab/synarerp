"""حساب مكافأة نهاية الخدمة وفق نظام العمل السعودي (المواد 84 و85 و87 و80)."""
import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db import models

from core.utils import flt

DAYS_PER_YEAR = Decimal("365")


class SeparationReason(models.TextChoices):
    TERMINATION = "termination", "إنهاء العقد من صاحب العمل (مادة 84)"
    CONTRACT_END = "contract_end", "انتهاء مدة العقد أو التقاعد (مادة 84)"
    RESIGNATION = "resignation", "استقالة الموظف (مادة 85)"
    ARTICLE_87 = "article_87", "ترك العمل لقوة قاهرة أو للعاملة بعد الزواج/الوضع (مادة 87)"
    DEATH = "death", "وفاة الموظف"
    ARTICLE_80 = "article_80", "فصل وفق المادة 80 (بدون مكافأة)"


FULL_AWARD_REASONS = {SeparationReason.TERMINATION, SeparationReason.CONTRACT_END, SeparationReason.ARTICLE_87,
                      SeparationReason.DEATH}


def service_parts(start, end):
    """مدة الخدمة (سنوات، أشهر، أيام) من تاريخ التعيين حتى آخر يوم عمل شاملاً."""
    if end < start:
        return 0, 0, 0
    stop = end + datetime.timedelta(days=1)
    years, months, days = stop.year - start.year, stop.month - start.month, stop.day - start.day
    if days < 0:
        months -= 1
        previous = stop.replace(day=1) - datetime.timedelta(days=1)
        days = max(days + previous.day, 0)
    if months < 0:
        years -= 1
        months += 12
    return years, months, days


def service_years(start, end):
    """مدة الخدمة بالسنوات: السنوات الكاملة + الأشهر ÷ 12 + الأيام ÷ 365."""
    years, months, days = service_parts(start, end)
    return Decimal(years) + Decimal(months) / 12 + Decimal(days) / DAYS_PER_YEAR


def full_award(years, wage):
    """نصف أجر شهر عن كل سنة من السنوات الخمس الأولى، وأجر شهر عن كل سنة بعدها، ولأجزاء السنة بنسبتها."""
    wage = Decimal(wage)
    first = min(years, Decimal(5)) * wage / 2
    rest = max(years - Decimal(5), Decimal(0)) * wage
    return flt(first + rest)


def resignation_ratio(years):
    """المادة 85: أقل من سنتين لا شيء، 2-5 ثلث، 5-10 ثلثان، 10 فأكثر كاملة."""
    if years < 2:
        return Decimal(0)
    if years < 5:
        return Decimal(1) / 3
    if years < 10:
        return Decimal(2) / 3
    return Decimal(1)


def entitlement_ratio(reason, years):
    if reason in FULL_AWARD_REASONS:
        return Decimal(1)
    if reason == SeparationReason.RESIGNATION:
        return resignation_ratio(years)
    return Decimal(0)


@dataclass
class EOSResult:
    years: Decimal
    wage: Decimal
    full: Decimal
    ratio: Decimal
    award: Decimal


def calculate(start, end, wage, reason=SeparationReason.TERMINATION):
    years = service_years(start, end)
    full = full_award(years, wage)
    ratio = entitlement_ratio(reason, years)
    return EOSResult(years=years, wage=flt(wage), full=full, ratio=ratio, award=flt(full * ratio))


def describe_duration(start, end):
    years, months, days = service_parts(start, end)
    parts = [f"{n} {label}" for n, label in ((years, "سنة"), (months, "شهر"), (days, "يوم")) if n]
    return " و".join(parts) or "—"
