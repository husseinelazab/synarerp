"""بيانات تجريبية لمعيار حوكمة المركز الوطني: تحميل القالب وتقييم ذاتي بمؤشرات مقيّمة جزئياً (idempotent)."""

DEMO_NOTE = "تقييم ذاتي تجريبي وفق معيار المركز الوطني"

# مؤشرات مقيّمة يدوياً في التقييم التجريبي (الباقي يُترك ليظهر في تقرير الجاهزية كفجوات)
DEMO_SCORES = {
    "N1.01": ("met", "اللائحة الأساسية المعتمدة من المركز"),
    "N1.02": ("met", "باب العضوية في اللائحة"),
    "N1.14": ("partial", "عُقدت 3 اجتماعات للمجلس"),
    "N1.20": ("not_met", "السياسة قيد الإعداد"),
    "N2.01": ("met", "الموقع الإلكتروني للجمعية"),
    "N2.18": ("partial", "القوائم منشورة دون تقرير المراجع"),
    "N3.07": ("met", "اللائحة المالية المعتمدة"),
    "N3.13": ("not_met", "لم تُعد موازنة تقديرية"),
}


def seed_demo(company):
    """يحمّل معيار المركز للجهة وينشئ تقييماً ذاتياً تجريبياً واحداً لسنة التقييم السابقة. يعيد التقييم."""
    from django.utils import timezone

    from core.tenancy import tenant_context
    from governance.models import FrameworkSource, GovernanceAssessment
    from governance.ncnp import load_framework

    load_framework(company)
    with tenant_context(company):
        existing = GovernanceAssessment.objects.filter(framework=FrameworkSource.NCNP, notes=DEMO_NOTE).first()
        if existing is not None:
            return existing
        assessment = GovernanceAssessment.objects.create(
            company=company, framework=FrameworkSource.NCNP, year=timezone.localdate().year - 1, notes=DEMO_NOTE)
        assessment.calculate_totals()
        assessment.save()
        for row in assessment.scores.select_related("indicator"):
            if row.indicator.code in DEMO_SCORES:
                row.status, row.note = DEMO_SCORES[row.indicator.code]
                row.save(update_fields=["status", "note"])
        assessment.calculate_totals()
        assessment.save()
        return assessment
