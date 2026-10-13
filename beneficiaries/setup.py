"""البيانات الأساسية للجهة غير الربحية: فئات المستفيدين ومعايير دراسة الحالة."""

DEFAULT_CATEGORIES = ["أسر محتاجة", "أيتام", "أرامل", "مطلقات", "كبار السن", "ذوو الإعاقة", "مرضى", "غارمون"]

# مجموع الدرجات 100
DEFAULT_CRITERIA = [
    ("دخل الفرد الشهري", 30, "كلما قل دخل الفرد بعد الالتزامات زادت الدرجة"),
    ("السكن", 20, "الإيجار أو السكن غير الملائم يرفع الدرجة"),
    ("عدد المعالين", 15, "الأطفال وكبار السن وغير القادرين على العمل"),
    ("الحالة الصحية", 15, "الأمراض المزمنة والإعاقات في الأسرة"),
    ("الديون والالتزامات", 10, "الديون الموثقة والالتزامات الشهرية"),
    ("تقدير الباحث الاجتماعي", 10, "انطباع الباحث من الزيارة الميدانية"),
]


def ensure_nonprofit_setup(company):
    """تُنشأ مرة واحدة: لا تعدّل ولا تكرر ما أضافته الجهة أو حذفته بعد ذلك."""
    from beneficiaries.models import AssessmentCriterion, BeneficiaryCategory

    if BeneficiaryCategory.all_objects.filter(company=company).exists():
        return
    BeneficiaryCategory.all_objects.bulk_create(
        [BeneficiaryCategory(company=company, name=name) for name in DEFAULT_CATEGORIES])
    AssessmentCriterion.all_objects.bulk_create([
        AssessmentCriterion(company=company, name=name, max_score=score, description=description, sort_order=i)
        for i, (name, score, description) in enumerate(DEFAULT_CRITERIA, 1)])
