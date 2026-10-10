"""كتابة المبالغ بالحروف العربية (التفقيط) لطباعة الفواتير: «فقط ألف ومائتان وخمسة وستون ريالاً سعودياً لا غير»."""
from decimal import ROUND_HALF_UP, Decimal

ONES_M = ["", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة"]
ONES_F = ["", "واحدة", "اثنتان", "ثلاث", "أربع", "خمس", "ست", "سبع", "ثماني", "تسع"]
TEENS_M = ["عشرة", "أحد عشر", "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر", "ستة عشر", "سبعة عشر",
           "ثمانية عشر", "تسعة عشر"]
TEENS_F = ["عشر", "إحدى عشرة", "اثنتا عشرة", "ثلاث عشرة", "أربع عشرة", "خمس عشرة", "ست عشرة", "سبع عشرة",
           "ثماني عشرة", "تسع عشرة"]
TENS = ["", "", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
HUNDREDS = ["", "مائة", "مائتان", "ثلاثمائة", "أربعمائة", "خمسمائة", "ستمائة", "سبعمائة", "ثمانمائة", "تسعمائة"]
# (مفرد، مثنى، جمع 3-10، تمييز 11-99)
SCALES = [
    ("مليار", "ملياران", "مليارات", "ملياراً", 10 ** 9),
    ("مليون", "مليونان", "ملايين", "مليوناً", 10 ** 6),
    ("ألف", "ألفان", "آلاف", "ألفاً", 10 ** 3),
]
CURRENCIES = {
    "SAR": (("ريال سعودي", "ريالان سعوديان", "ريالات سعودية", "ريالاً سعودياً"), False,
            ("هللة", "هللتان", "هللات", "هللة"), True),
    "USD": (("دولار أمريكي", "دولاران أمريكيان", "دولارات أمريكية", "دولاراً أمريكياً"), False,
            ("سنت", "سنتان", "سنتات", "سنتاً"), False),
    "EUR": (("يورو",) * 4, False, ("سنت", "سنتان", "سنتات", "سنتاً"), False),
    "AED": (("درهم إماراتي", "درهمان إماراتيان", "دراهم إماراتية", "درهماً إماراتياً"), False,
            ("فلس", "فلسان", "فلوس", "فلساً"), False),
    "KWD": (("دينار كويتي", "ديناران كويتيان", "دنانير كويتية", "ديناراً كويتياً"), False,
            ("فلس", "فلسان", "فلوس", "فلساً"), False),
    "EGP": (("جنيه مصري", "جنيهان مصريان", "جنيهات مصرية", "جنيهاً مصرياً"), False,
            ("قرش", "قرشان", "قروش", "قرشاً"), False),
}


def _below_thousand(n, feminine=False):
    ones, teens = (ONES_F, TEENS_F) if feminine else (ONES_M, TEENS_M)
    parts = []
    if n >= 100:
        parts.append(HUNDREDS[n // 100])
        n %= 100
    if 10 <= n < 20:
        parts.append(teens[n - 10])
    elif n:
        unit, ten = n % 10, n // 10
        if unit and ten:
            parts.append(f"{ones[unit]} و{TENS[ten]}")
        else:
            parts.append(ones[unit] or TENS[ten])
    return " و".join(parts)


def _counted(n, forms, feminine=False):
    """العدد مع المعدود حسب قواعد التمييز."""
    singular, dual, plural, accusative = forms
    if n == 1:
        return f"{singular} {'واحدة' if feminine else 'واحد'}"
    if n == 2:
        return dual
    words = number_to_words(n, feminine)
    rest = n % 100
    if 3 <= rest <= 10:
        return f"{words} {plural}"
    if 11 <= rest <= 99:
        return f"{words} {accusative}"
    return f"{words} {singular}"


def number_to_words(n, feminine=False):
    n = int(n)
    if n == 0:
        return "صفر"
    parts = []
    for singular, dual, plural, accusative, size in SCALES:
        count, n = divmod(n, size)
        if not count:
            continue
        if count == 1:
            parts.append(singular)
        elif count == 2:
            parts.append(dual)
        elif 3 <= count % 100 <= 10:
            parts.append(f"{_below_thousand(count)} {plural}")
        elif count % 100 >= 11:
            parts.append(f"{number_to_words(count)} {accusative}")
        else:
            parts.append(f"{number_to_words(count)} {singular}")
    if n:
        parts.append(_below_thousand(n, feminine))
    return " و".join(parts)


def amount_in_words(amount, currency_code="SAR", currency_name=""):
    amount = abs(Decimal(str(amount or 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    major, minor = int(amount), int((amount - int(amount)) * 100)
    if currency_code in CURRENCIES:
        major_forms, major_fem, minor_forms, minor_fem = CURRENCIES[currency_code]
    else:
        name = currency_name or currency_code
        major_forms, major_fem, minor_forms, minor_fem = (name,) * 4, False, ("جزء", "جزءان", "أجزاء", "جزءاً"), False
    parts = []
    if major or not minor:
        parts.append(_counted(major, major_forms, major_fem) if major else f"صفر {major_forms[0]}")
    if minor:
        parts.append(_counted(minor, minor_forms, minor_fem))
    return "فقط " + " و".join(parts) + " لا غير"
