"""بيانات تجريبية لإدارة المنح: جهة مانحة، ومنحة معتمدة بصندوقها المقيد وموازنتها ودفعاتها وتقاريرها."""
import datetime
from decimal import Decimal

GRANT_TITLE = "مشروع السلة الغذائية للأسر المتعففة"


def seed_demo(company, today):
    """تُنشئ بيانات المنح التجريبية مرة واحدة (لا تتكرر إن وُجدت المنحة). تعيد المنحة."""
    from accounts.models import Account, JournalEntry
    from core.tenancy import tenant_context
    from funds.models import Donor, DonorType
    from grants.models import ApplicationStatus, Grant, GrantApplication, ReportKind

    with tenant_context(company):
        existing = Grant.objects.filter(title=GRANT_TITLE).first()
        if existing is not None:
            return existing
        grantor, _ = Donor.objects.get_or_create(name="مؤسسة الملك خالد الخيرية", defaults={
            "donor_type": DonorType.ENTITY, "phone": "0114811111", "email": "grants@kkf.example", "city": "الرياض",
            "notes": "جهة مانحة — منح البرامج التنموية"})
        start = today - datetime.timedelta(days=60)
        application = GrantApplication.objects.create(
            grantor=grantor, title=GRANT_TITLE, requested_amount=Decimal("200000"),
            submitted_date=start - datetime.timedelta(days=45), status=ApplicationStatus.APPROVED,
            decision_date=start, notes="طلب دعم توزيع سلال غذائية شهرية لـ 150 أسرة لمدة عام")
        grant = application.create_grant(None)
        grant.agreement_number = "KKF-2026-117"
        grant.notes = "تقرير مالي ربع سنوي وتقرير فني نهائي؛ لا تُنقل المبالغ بين البنود بأكثر من 10% إلا بموافقة المانح."
        grant.save(update_fields=["agreement_number", "notes"])
        grant.create_fund()
        accounts = {a.number: a for a in Account.objects.filter(number__in=["42102004", "42102002", "41101001"])}
        for description, number, amount in (("السلال الغذائية", "42102004", 150000),
                                            ("مساعدات نقدية طارئة", "42102002", 35000),
                                            ("رواتب منسق المشروع", "41101001", 15000)):
            if number in accounts:
                grant.budget_lines.create(description=description, account=accounts[number], amount=amount)
        received = grant.installments.create(due_date=start + datetime.timedelta(days=7), amount=Decimal("100000"),
                                             company=company, notes="الدفعة الأولى عند توقيع الاتفاقية")
        grant.installments.create(due_date=today + datetime.timedelta(days=60), amount=Decimal("100000"),
                                  company=company, notes="الدفعة الثانية بعد اعتماد التقرير المالي الأول")
        received.receive(None, posting_date=today)
        if "42102004" in accounts:  # صرف من المنحة على بند السلال ليظهر الفعلي في الموازنة
            entry = JournalEntry.objects.create(posting_date=today, remarks=f"شراء سلال غذائية — {GRANT_TITLE}")
            entry.accounts.create(account=accounts["42102004"], debit=Decimal("27500"), fund=grant.fund)
            entry.accounts.create(account=company.default_cash_account, credit=Decimal("27500"))
            entry.calculate_totals()
            entry.save()
            entry.submit()
        grant.reports.create(kind=ReportKind.FINANCIAL, period_start=start,
                             period_end=start + datetime.timedelta(days=89),
                             due_date=today + datetime.timedelta(days=5), company=company)
        grant.reports.create(kind=ReportKind.NARRATIVE, period_start=start, period_end=grant.end_date,
                             due_date=grant.end_date + datetime.timedelta(days=30), company=company)
        return grant
