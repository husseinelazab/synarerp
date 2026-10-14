"""تنبيهات المنح لمديري الجمعية: تقارير المانح المستحقة خلال 7 أيام أو المتأخرة، ودفعات المنح المتأخرة.

يُستدعى يومياً من المجدول: ``from grants.alerts import grants_alerts`` ثم ``grants_alerts(company)``،
أو لكل الجمعيات: ``python manage.py grants_alerts``. لا يتكرر التنبيه نفسه (يُفحص بعنوانه).
"""
from django.utils import timezone

from core.models import Notification
from core.notify import notify_managers
from core.tenancy import tenant_context


def grants_alerts(company, today=None):
    """يرسل التنبيهات الجديدة لمديري الشركة ويعيد عددها."""
    from grants.models import GrantInstallment, GrantReport, GrantStatus, ReportStatus

    today = today or timezone.localdate()
    sent = 0
    with tenant_context(company):
        def alert(title, url):
            nonlocal sent
            if not Notification.objects.filter(title=title).exists():
                notify_managers(company, title, url)
                sent += 1

        reports = GrantReport.objects.filter(status=ReportStatus.PENDING, grant__status=GrantStatus.ACTIVE,
                                             due_date__lte=today + timezone.timedelta(days=7)).select_related("grant")
        for report in reports:
            if report.due_date < today:
                title = (f"تقرير متأخر للمانح: {report.get_kind_display()} لمنحة «{report.grant}» "
                         f"كان موعده {report.due_date:%Y-%m-%d}")
            else:
                title = (f"تقرير للمانح مستحق في {report.due_date:%Y-%m-%d}: {report.get_kind_display()} "
                         f"لمنحة «{report.grant}»")
            alert(title[:255], report.get_absolute_url())
        installments = GrantInstallment.objects.filter(receipt__isnull=True, grant__status=GrantStatus.ACTIVE,
                                                       due_date__lt=today).select_related("grant__grantor")
        for installment in installments:
            title = (f"دفعة منحة متأخرة: {installment.amount:,.2f} من {installment.grant.grantor} "
                     f"لمنحة «{installment.grant}» كانت مستحقة في {installment.due_date:%Y-%m-%d}")
            alert(title[:255], installment.get_absolute_url())
    return sent
