"""التاريخ الهجري وفق تقويم أم القرى (مكتبة hijridate)."""
import datetime

from hijridate import Gregorian, Hijri

HIJRI_MONTHS = ["محرم", "صفر", "ربيع الأول", "ربيع الآخر", "جمادى الأولى", "جمادى الآخرة",
                "رجب", "شعبان", "رمضان", "شوال", "ذو القعدة", "ذو الحجة"]


def to_hijri(date):
    """(السنة، الشهر، اليوم) الهجري لتاريخ ميلادي، أو None خارج نطاق التقويم."""
    try:
        h = Gregorian(date.year, date.month, date.day).to_hijri()
    except (ValueError, OverflowError):
        return None
    return h.year, h.month, h.day


def format_hijri(date):
    parts = to_hijri(date) if date else None
    if parts is None:
        return ""
    year, month, day = parts
    return f"{day} {HIJRI_MONTHS[month - 1]} {year}هـ"


def to_gregorian(year, month, day):
    g = Hijri(year, month, day).to_gregorian()
    return datetime.date(g.year, g.month, g.day)


def eid_holidays(gregorian_year):
    """عطلتا العيدين في السنة الميلادية حسب أم القرى، وفق اللائحة التنفيذية لنظام العمل (المادة 112):
    عيد الفطر 4 أيام تبدأ من اليوم التالي لـ 29 رمضان، وعيد الأضحى 4 أيام تبدأ من يوم عرفة (9 ذو الحجة).
    تبقى تقديرية حتى إعلان الجهات الرسمية (رؤية الهلال)."""
    result = []
    first = to_hijri(datetime.date(gregorian_year, 1, 1))[0]
    for hijri_year in (first, first + 1):
        try:
            fitr = to_gregorian(hijri_year, 9, 29) + datetime.timedelta(days=1)
            adha = to_gregorian(hijri_year, 12, 9)
        except (ValueError, OverflowError):
            continue
        for name, start in ((f"عيد الفطر {hijri_year}هـ", fitr), (f"عيد الأضحى {hijri_year}هـ", adha)):
            if start.year == gregorian_year:
                result.append((name, start, start + datetime.timedelta(days=3)))
    return sorted(result, key=lambda r: r[1])
