"""فئات ضريبة القيمة المضافة وأسباب الإعفاء وفق رموز هيئة الزكاة والضريبة والجمارك (UNCL5305 / VATEX)."""
from django.db import models


class VATCategory(models.TextChoices):
    STANDARD = "S", "خاضع للنسبة الأساسية"
    ZERO = "Z", "خاضع بنسبة صفر"
    EXEMPT = "E", "معفى من الضريبة"
    OUT_OF_SCOPE = "O", "خارج نطاق الضريبة"


EXEMPTION_REASONS = {
    VATCategory.ZERO: [
        ("VATEX-SA-32", "صادرات السلع من المملكة"),
        ("VATEX-SA-33", "صادرات الخدمات من المملكة"),
        ("VATEX-SA-34-1", "النقل الدولي للسلع"),
        ("VATEX-SA-34-2", "النقل الدولي للركاب"),
        ("VATEX-SA-34-3", "الخدمات المرتبطة مباشرة أو عرضياً بتوريد النقل الدولي للركاب"),
        ("VATEX-SA-34-4", "توريد وسائل النقل المؤهلة"),
        ("VATEX-SA-34-5", "الخدمات ذات الصلة بالسلع أو الركاب المنقولين"),
        ("VATEX-SA-35", "الأدوية والمعدات الطبية"),
        ("VATEX-SA-36", "المعادن الاستثمارية المؤهلة"),
        ("VATEX-SA-EDU", "الخدمات التعليمية الخاصة للمواطنين"),
        ("VATEX-SA-HEA", "الخدمات الصحية الخاصة للمواطنين"),
        ("VATEX-SA-MLTRY", "توريد السلع العسكرية المؤهلة"),
    ],
    VATCategory.EXEMPT: [
        ("VATEX-SA-29", "الخدمات المالية"),
        ("VATEX-SA-29-7", "عقود التأمين على الحياة"),
        ("VATEX-SA-30", "التوريدات العقارية المعفاة"),
    ],
    VATCategory.OUT_OF_SCOPE: [
        ("VATEX-SA-OOS", "خارج نطاق ضريبة القيمة المضافة"),
    ],
}

REASON_CHOICES = [(code, f"{code} — {text}") for reasons in EXEMPTION_REASONS.values() for code, text in reasons]
REASON_TEXT = {code: text for reasons in EXEMPTION_REASONS.values() for code, text in reasons}
REASON_CATEGORY = {code: category for category, reasons in EXEMPTION_REASONS.items() for code, _ in reasons}


def validate_category(category, reason, label="المستند"):
    """يعيد رسالة خطأ أو None."""
    if category == VATCategory.STANDARD:
        return None if not reason else f"{label}: سبب الإعفاء يُستخدم مع الفئات الصفرية والمعفاة وخارج النطاق فقط."
    if not reason:
        return f"{label}: حدد سبب الإعفاء أو النسبة الصفرية (مطلوب في الفوترة الإلكترونية)."
    if REASON_CATEGORY.get(reason) != category:
        return f"{label}: السبب {reason} لا يتوافق مع فئة «{VATCategory(category).label}»."
    return None
