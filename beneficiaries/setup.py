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


DEFAULT_FUNDS = [("صندوق الزكاة", "restricted", "تُصرف الزكاة في مصارفها الشرعية فقط"),
                 ("صندوق الكفالات", "restricted", "مبالغ الكافلين تُصرف لمكفوليهم")]


# برامج المساعدات: (الاسم، النوع، حساب المصروف من الدليل الموحد، الصندوق)
DEFAULT_PROGRAMS = [
    ("مساعدة نقدية", "cash", "42102002", None),
    ("صرف الزكاة", "cash", "42101001", "صندوق الزكاة"),
    ("السلة الغذائية", "in_kind", "42103003", None),
    ("الكفالات", "cash", "42102001", "صندوق الكفالات"),
]
# أنواع الكفالات: (الاسم، فئة المكفولين أو None)
DEFAULT_SPONSORSHIP_TYPES = [("كفالة يتيم", "أيتام"), ("كفالة أسرة", None), ("كفالة طالب علم", None)]


def ensure_aid_programs(company):
    """تُنشأ مرة واحدة بعد تأسيس الدليل الموحد للجهات غير الربحية."""
    from accounts.chart import uses_nonprofit_chart
    from accounts.models import Account, Fund
    from beneficiaries.models import AidProgram

    if AidProgram.all_objects.filter(company=company).exists() or not uses_nonprofit_chart(company):
        return
    accounts = {a.number: a for a in Account.all_objects.filter(company=company, number__in=[
        number for _, _, number, _ in DEFAULT_PROGRAMS])}
    funds = {f.name: f for f in Fund.all_objects.filter(company=company)}
    programs = {}
    for name, aid_type, number, fund in DEFAULT_PROGRAMS:
        if number in accounts:
            programs[name] = AidProgram.all_objects.create(company=company, name=name, aid_type=aid_type,
                                                           expense_account=accounts[number], fund=funds.get(fund))
    if "الكفالات" in programs:
        from beneficiaries.models import BeneficiaryCategory
        from funds.models import SponsorshipType

        for name, category in DEFAULT_SPONSORSHIP_TYPES:
            kind = SponsorshipType.all_objects.create(company=company, name=name, aid_program=programs["الكفالات"])
            if category:
                kind.categories.set(BeneficiaryCategory.all_objects.filter(company=company, name=category))


def ensure_nonprofit_setup(company):
    from beneficiaries.models import DependentCategory
    for label in ("طالب", "موظف", "ذو إعاقة", "يتيم", "أرمل / أرملة", "متقاعد", "عاطل عن العمل"):
        DependentCategory.objects.get_or_create(company=company, name=label)

    """تُنشأ مرة واحدة: لا تعدّل ولا تكرر ما أضافته الجهة أو حذفته بعد ذلك.

    دليل الحسابات الموحد للجهات غير الربحية يُؤسس عند إنشاء الجمعية (core.provisioning).
    """
    from accounts.models import Fund
    from beneficiaries.models import AssessmentCriterion, BeneficiaryCategory

    ensure_aid_programs(company)
    from governance.setup import ensure_governance_setup

    ensure_governance_setup(company)
    from office.setup import ensure_office_setup

    ensure_office_setup(company)
    if BeneficiaryCategory.all_objects.filter(company=company).exists():
        return
    for name, fund_type, purpose in DEFAULT_FUNDS:
        Fund.all_objects.get_or_create(company=company, name=name,
                                       defaults={"fund_type": fund_type, "purpose": purpose})
    BeneficiaryCategory.all_objects.bulk_create(
        [BeneficiaryCategory(company=company, name=name) for name in DEFAULT_CATEGORIES])
    AssessmentCriterion.all_objects.bulk_create([
        AssessmentCriterion(company=company, name=name, max_score=score, description=description, sort_order=i)
        for i, (name, score, description) in enumerate(DEFAULT_CRITERIA, 1)])
