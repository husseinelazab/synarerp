"""البيانات الأساسية للأعضاء والحوكمة عند تأسيس الجهة غير الربحية."""

DEFAULT_CATEGORIES = [("عضو عامل", 0, True), ("عضو منتسب", 0, False), ("عضو شرفي", 0, False)]

# معايير مؤشر الحوكمة الثلاثة؛ المؤشرات أدناه ممارسات تُقاس آلياً من النظام — أضف مؤشرات نموذج المركز الوطني
# المعتمد لديك (أو استوردها من Excel) وعدّل الأوزان.
DEFAULT_STANDARDS = [("1", "الالتزام والامتثال"), ("2", "الشفافية والإفصاح"), ("3", "السلامة المالية")]
DEFAULT_INDICATORS = [
    ("1", "1.A1", "عقد اجتماع الجمعية العمومية العادية خلال السنة واعتماد محضره", "ga_held", 1, "محضر الاجتماع وكشف الحضور"),
    ("1", "1.A2", "انتظام اجتماعات مجلس الإدارة خلال السنة", "board_meetings", 4, "محاضر اجتماعات المجلس"),
    ("1", "1.A3", "وجود مجلس إدارة بدورة سارية ومعتمدة", "board_term", 0, "خطاب اعتماد المجلس"),
    ("1", "1.A4", "متابعة تنفيذ قرارات المجلس والجمعية العمومية في مواعيدها", "decisions", 0, "سجل القرارات"),
    ("2", "2.A1", "تحديث دراسات حالة المستفيدين المعتمدين في مواعيدها", "case_studies", 90, "دراسات الحالة"),
    ("3", "3.A1", "التزام الصرف من الأموال المقيدة في حدود أرصدتها وأغراضها", "funds", 0, "تقرير أرصدة الصناديق"),
]


def ensure_governance_setup(company):
    from governance.models import GovernanceIndicator, GovernanceStandard, MembershipCategory

    if MembershipCategory.all_objects.filter(company=company).exists():
        return
    MembershipCategory.all_objects.bulk_create([MembershipCategory(company=company, name=n, annual_fee=fee,
                                                                   has_voting_rights=vote)
                                                for n, fee, vote in DEFAULT_CATEGORIES])
    standards = {code: GovernanceStandard.all_objects.create(company=company, code=code, name=name)
                 for code, name in DEFAULT_STANDARDS}
    GovernanceIndicator.all_objects.bulk_create([
        GovernanceIndicator(company=company, standard=standards[s], code=code, text=text, auto_check=check,
                            auto_target=target, evidence_hint=hint)
        for s, code, text, check, target, hint in DEFAULT_INDICATORS])
