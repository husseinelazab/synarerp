"""التحقق المقترح لمؤشرات الحوكمة من بيانات سِينار.

كل دالة مسجلة بمفتاح (check_key في المؤشر) وتعيد Suggestion(الحالة المقترحة، الشرح). الاقتراح لا يعتمد الدرجة؛
المستخدم يؤكدها في التقييم الذاتي. المفاتيح غير المعروفة تُتجاهل. تعمل الدوال داخل سياق الشركة الحالية
(المديرون يرشّحون بالشركة تلقائياً).
"""
import datetime
import re
from collections import namedtuple

from django.utils import timezone

Suggestion = namedtuple("Suggestion", "status text")
Check = namedtuple("Check", "key label func fix")

MET, PARTIAL, NOT_MET, NA = "met", "partial", "not_met", "na"

CHECKS = {}


def register(key, label, fix=""):
    """يسجل دالة تحقق: func(year, target) -> Suggestion. fix: مكان المعالجة الافتراضي (مثل doc_list:meeting)."""
    def decorator(func):
        CHECKS[key] = Check(key, label, func, fix)
        return func
    return decorator


def run_check(key, year, target=0):
    check = CHECKS.get(key)
    if check is None:
        return None
    return check.func(year, target or 0)


def check_choices():
    return [(k, c.label) for k, c in CHECKS.items()]


_ARABIC_DIACRITICS = re.compile("[ً-ْـ]")


def normalize(text):
    """توحيد الكتابة العربية للمطابقة بالكلمات المفتاحية (الهمزات والتاء المربوطة والتشكيل)."""
    text = _ARABIC_DIACRITICS.sub("", (text or "").lower())
    return (text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي"))


def _documents(category=None):
    from website.models import PublishedDocument

    docs = PublishedDocument.objects.filter(is_published=True, size__gt=0).defer("content")
    return docs.filter(category=category) if category else docs


def _website():
    from website.models import Website

    return Website.objects.first()


# ───────────────────────── مجلس الإدارة والجمعية العمومية ─────────────────────────

@register("board_term_current", "دورة مجلس إدارة سارية بعدد أعضاء لا يقل عن الحد المطلوب", "doc_list:board-term")
def board_term_current(year, target):
    from governance.models import BoardTerm

    term = BoardTerm.current()
    if term is None:
        return Suggestion(NOT_MET, "لا توجد دورة مجلس إدارة سارية اليوم.")
    seats = term.seats.count()
    needed = target or 5
    text = f"الدورة السارية «{term}» حتى {term.end_date:%Y-%m-%d} بعدد {seats} عضواً (المطلوب {needed} على الأقل)."
    return Suggestion(MET if seats >= needed else PARTIAL, text)


@register("board_meetings", "اجتماعات مجلس الإدارة المعتمدة خلال السنة", "doc_new:meeting")
def board_meetings(year, target):
    from governance.models import Meeting, MeetingType

    needed = target or 4
    count = Meeting.objects.filter(docstatus=1, meeting_type=MeetingType.BOARD, posting_date__year=year).count()
    status = MET if count >= needed else PARTIAL if count else NOT_MET
    return Suggestion(status, f"اجتماعات مجلس إدارة معتمدة المحضر في {year}: {count} (المطلوب {needed}).")


@register("ga_meeting_12m", "اجتماع جمعية عمومية عادية بمحضر معتمد خلال آخر 12 شهراً", "doc_new:meeting")
def ga_meeting_12m(year, target):
    from governance.models import Meeting, MeetingType

    since = timezone.localdate() - datetime.timedelta(days=365)
    meetings = Meeting.objects.filter(meeting_type=MeetingType.GA_ORDINARY, posting_date__gte=since)
    done = [m for m in meetings.filter(docstatus=1) if m.minutes.strip()]
    if done:
        last = max(done, key=lambda m: m.posting_date)
        return Suggestion(MET, f"آخر جمعية عمومية عادية معتمدة المحضر: {last.name} بتاريخ {last.posting_date:%Y-%m-%d}.")
    if meetings.filter(docstatus=0).exists():
        return Suggestion(PARTIAL, "يوجد اجتماع جمعية عمومية عادية خلال 12 شهراً لكن محضره غير معتمد بعد.")
    return Suggestion(NOT_MET, f"لا يوجد اجتماع جمعية عمومية عادية منذ {since:%Y-%m-%d}.")


@register("members_register", "سجل أعضاء الجمعية العمومية", "doc_list:member")
def members_register(year, target):
    from governance.models import Member, MemberStatus

    active = Member.objects.filter(status=MemberStatus.ACTIVE).count()
    return Suggestion(MET if active else NOT_MET, f"أعضاء نشطون مسجلون في سجل الأعضاء: {active}.")


@register("member_growth", "انضمام أعضاء جدد خلال السنة", "doc_list:member")
def member_growth(year, target):
    from governance.models import Member

    joined = Member.objects.filter(join_date__year=year).count()
    return Suggestion(MET if joined else NOT_MET, f"أعضاء انضموا خلال {year}: {joined}.")


@register("member_fees_recorded", "تسجيل اشتراكات الأعضاء ومتابعتها", "members_dashboard")
def member_fees_recorded(year, target):
    from governance.models import FeeStatus, MembershipCategory, MembershipFee

    if not MembershipCategory.objects.filter(annual_fee__gt=0).exists():
        return Suggestion(NA, "لا توجد فئة عضوية باشتراك سنوي؛ حدّد الاشتراك في فئات العضوية إن وُجد.")
    fees = MembershipFee.objects.filter(year=year)
    total = fees.count()
    if not total:
        return Suggestion(NOT_MET, f"لم تُنشأ اشتراكات {year} في سجل الاشتراكات.")
    settled = fees.filter(status__in=[FeeStatus.PAID, FeeStatus.WAIVED]).count()
    return Suggestion(MET, f"اشتراكات {year} مسجلة: {total}، المسدد أو المعفى منها {settled}.")


@register("membership_open", "استقبال طلبات العضوية من الموقع", "website_settings")
def membership_open(year, target):
    site = _website()
    if site is None:
        return Suggestion(NOT_MET, "لا يوجد موقع إلكتروني للجمعية في النظام.")
    if site.is_published and site.allow_membership_requests:
        return Suggestion(MET, "الموقع منشور ويستقبل طلبات العضوية.")
    return Suggestion(PARTIAL, "الموقع غير منشور أو لا يستقبل طلبات العضوية؛ تحقق من نص اللائحة.")


# ───────────────────────── الإفصاح على الموقع ─────────────────────────

@register("website_published", "موقع إلكتروني منشور للجمعية", "website_settings")
def website_published(year, target):
    site = _website()
    if site is None:
        return Suggestion(NOT_MET, "لا يوجد موقع إلكتروني للجمعية في النظام.")
    if site.is_published:
        return Suggestion(MET, f"الموقع منشور: {site}.")
    return Suggestion(PARTIAL, "الموقع مُعدّ لكنه غير منشور للعامة.")


@register("website_disclosure", "صفحة الحوكمة والإفصاح في الموقع", "doc_list:published-document")
def website_disclosure(year, target):
    site = _website()
    count = _documents().count()
    if site is None or not site.is_published:
        return Suggestion(PARTIAL if count else NOT_MET, f"الموقع غير منشور؛ وثائق الإفصاح المرفوعة: {count}.")
    return Suggestion(MET if count else PARTIAL, f"صفحة الحوكمة في الموقع تعرض {count} وثيقة منشورة.")


def _year_document(category, label):
    def check(year, target):
        docs = list(_documents(category).filter(year__gte=year - 1).values_list("title", "year"))
        current = [t for t, y in docs if y >= year]
        if current:
            return Suggestion(MET, f"{label} لعام {year} منشورة: «{current[0]}».")
        if docs:
            return Suggestion(PARTIAL, f"المنشور {label} لعام {year - 1} فقط: «{docs[0][0]}».")
        return Suggestion(NOT_MET, f"لا توجد {label} منشورة بملف لعام {year} في وثائق الحوكمة والإفصاح.")
    return check


register("financials_published", "نشر القوائم المالية المدققة لسنة التقييم",
         "doc_list:published-document")(_year_document("financials", "القوائم المالية"))
register("annual_report_published", "نشر التقرير السنوي لسنة التقييم",
         "doc_list:published-document")(_year_document("annual", "التقارير السنوية"))
register("ga_minutes_published", "نشر محاضر الجمعية العمومية",
         "doc_list:published-document")(_year_document("assembly", "محاضر الجمعية العمومية"))


@register("strategy_published", "نشر الخطة الاستراتيجية/التشغيلية", "doc_list:published-document")
def strategy_published(year, target):
    docs = list(_documents("strategy").values_list("title", flat=True))
    if docs:
        return Suggestion(MET, "خطط منشورة: " + "، ".join(f"«{t}»" for t in docs[:3]))
    return Suggestion(NOT_MET, "لا توجد خطة استراتيجية أو تشغيلية منشورة بملف في تصنيف «الخطط الاستراتيجية».")


@register("activity_reports_published", "نشر تقارير البرامج والأنشطة", "doc_list:news-post")
def activity_reports_published(year, target):
    from website.models import NewsPost

    reports = _documents("annual").filter(year__gte=year).count()
    news = NewsPost.objects.filter(is_published=True, published_on__year=year).count()
    if reports:
        return Suggestion(MET, f"تقارير سنوية منشورة لعام {year}: {reports}، وأخبار أنشطة: {news}.")
    if news:
        return Suggestion(PARTIAL, f"أخبار أنشطة منشورة في {year}: {news}، دون تقرير برامج منشور.")
    return Suggestion(NOT_MET, f"لا تقارير ولا أخبار أنشطة منشورة في {year}.")


@register("board_published", "عرض أعضاء مجلس الإدارة ومدة الدورة في الموقع", "doc_list:board-term")
def board_published(year, target):
    from governance.models import BoardTerm

    site = _website()
    term = BoardTerm.current()
    if term is None or not term.seats.exists():
        return Suggestion(NOT_MET, "لا توجد دورة مجلس سارية بأعضاء لعرضها في الموقع.")
    if site is None or not site.is_published:
        return Suggestion(PARTIAL, "بيانات المجلس مسجلة لكن الموقع غير منشور.")
    return Suggestion(MET, f"صفحتا «من نحن» و«الحوكمة» تعرضان أعضاء «{term}» ومدة الدورة.")


@register("impact_stats_published", "نشر إحصاءات الخدمات والمستفيدين", "doc_list:impact-counter")
def impact_stats_published(year, target):
    from website.models import ImpactCounter

    count = ImpactCounter.objects.filter(is_published=True).count()
    site = _website()
    if count and site is not None and site.is_published:
        return Suggestion(MET, f"عدادات أثر منشورة في الموقع: {count}.")
    if count:
        return Suggestion(PARTIAL, f"عدادات أثر معدّة ({count}) والموقع غير منشور.")
    return Suggestion(NOT_MET, "لا توجد إحصاءات منشورة عن الخدمات والمستفيدين.")


@register("fundraising_license_valid", "ترخيص جمع التبرعات ساري", "website_settings")
def fundraising_license_valid(year, target):
    site = _website()
    if site is None or not site.fundraising_license_number:
        return Suggestion(NOT_MET, "لم يُسجَّل رقم ترخيص جمع التبرعات في إعدادات الموقع.")
    expiry = site.fundraising_license_expiry
    if expiry is None:
        return Suggestion(PARTIAL, f"الترخيص {site.fundraising_license_number} مسجل دون تاريخ انتهاء؛ أدخل تاريخ الانتهاء.")
    if expiry >= timezone.localdate():
        return Suggestion(MET, f"الترخيص {site.fundraising_license_number} ساري حتى {expiry:%Y-%m-%d}.")
    return Suggestion(NOT_MET, f"الترخيص {site.fundraising_license_number} منتهٍ في {expiry:%Y-%m-%d}.")


# ───────────────────────── السياسات واللوائح (بالكلمات المفتاحية في العنوان) ─────────────────────────

POLICY_KEYWORDS = {
    "bylaw_published": ("اللائحة الأساسية", ["اللائحه الاساسيه", "النظام الاساسي"]),
    "policy_conflict_of_interest": ("سياسة تعارض المصالح", ["تعارض المصالح", "تضارب المصالح"]),
    "policy_whistleblowing": ("سياسة الإبلاغ عن المخالفات", ["الإبلاغ عن المخالفات", "المبلغين"]),
    "policy_privacy": ("سياسة خصوصية البيانات", ["خصوصية", "حماية البيانات"]),
    "policy_document_retention": ("سياسة الاحتفاظ بالوثائق وإتلافها", ["الاحتفاظ بالوثائق", "اتلاف"]),
    "policy_fundraising": ("سياسة جمع التبرعات", ["جمع التبرعات"]),
    "policy_remuneration": ("سياسة المكافآت والامتيازات", ["المكافات", "المكافئات", "الامتيازات"]),
    "policy_hr": ("لائحة الموارد البشرية", ["الموارد البشريه", "شوون الموظفين", "سلم الرواتب"]),
    "policy_financial": ("لائحة السياسات المالية", ["السياسات الماليه", "اللائحه الماليه", "الصلاحيات الماليه"]),
    "policy_procurement": ("لائحة المشتريات", ["المشتريات"]),
}


def _policy_check(label, keywords):
    keys = [normalize(k) for k in keywords]

    def check(year, target):
        for title, category in _documents().values_list("title", "category"):
            if any(k in normalize(title) for k in keys):
                return Suggestion(MET, f"«{title}» منشورة بملف في وثائق الحوكمة والإفصاح.")
        if label == "سياسة خصوصية البيانات":
            site = _website()
            if site is not None and site.is_published:
                return Suggestion(PARTIAL, "الموقع يعرض سياسة خصوصية (افتراضية أو مخصصة)؛ ارفع السياسة المعتمدة من المجلس.")
        return Suggestion(NOT_MET, f"لم تُنشر {label}: لا توجد وثيقة بعنوان يتضمن «{keywords[0]}».")
    return check


for _key, (_label, _keywords) in POLICY_KEYWORDS.items():
    register(_key, f"نشر {_label}", "doc_list:published-document")(_policy_check(_label, _keywords))


# ───────────────────────── المالية ─────────────────────────

def _fiscal_year(year):
    from core.models import FiscalYear

    return FiscalYear.objects.filter(end_date__year=year).order_by("-end_date").first()


@register("year_end_closed", "إقفال السنة المالية", "doc_list:period-closing")
def year_end_closed(year, target):
    from accounts.models import PeriodClosingVoucher

    fiscal = _fiscal_year(year)
    if fiscal is None:
        return Suggestion(NOT_MET, f"لا توجد سنة مالية تنتهي في {year}.")
    closed = fiscal.is_closed or PeriodClosingVoucher.objects.filter(fiscal_year=fiscal, docstatus=1).exists()
    if closed:
        return Suggestion(MET, f"السنة المالية {fiscal} مقفلة بقيد إقفال معتمد.")
    return Suggestion(NOT_MET, f"السنة المالية {fiscal} لم تُقفل بعد.")


@register("books_kept", "قيود محاسبية مسجلة خلال السنة", "doc_list:journal-entry")
def books_kept(year, target):
    from accounts.models import GLEntry

    count = GLEntry.objects.filter(is_cancelled=False, posting_date__year=year).count()
    return Suggestion(MET if count else NOT_MET, f"قيود دفتر الأستاذ المرحّلة في {year}: {count}.")


def _budget(year):
    from accounts.models import Budget

    fiscal = _fiscal_year(year)
    if fiscal is None:
        return Suggestion(NOT_MET, f"لا توجد سنة مالية تنتهي في {year}.")
    budget = Budget.objects.filter(fiscal_year=fiscal, disabled=False).first()
    if budget is None:
        return Suggestion(NOT_MET, f"لا توجد موازنة تقديرية للسنة المالية {fiscal}.")
    return Suggestion(MET, f"الموازنة «{budget.name}» للسنة المالية {fiscal}؛ تأكد من بنائها على الخطة التشغيلية.")


@register("budget_exists", "موازنة تقديرية لسنة التقييم", "doc_list:budget")
def budget_exists(year, target):
    return _budget(year)


@register("budget_next_year", "موازنة تقديرية للسنة التالية", "doc_list:budget")
def budget_next_year(year, target):
    return _budget(year + 1)


@register("funds_no_deficit", "لا عجز في الصناديق المقيدة", "doc_list:fund")
def funds_no_deficit(year, target):
    from accounts.models import Fund, FundType

    short = [f.name for f in Fund.objects.filter(fund_type=FundType.RESTRICTED) if f.balance < 0]
    if short:
        return Suggestion(NOT_MET, "صناديق مقيدة بعجز: " + "، ".join(short))
    return Suggestion(PARTIAL, "لا عجز في الصناديق المقيدة؛ احسب نسبة تغطية النقد للالتزامات من القوائم المالية.")
