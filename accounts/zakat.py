"""تقدير الوعاء الزكوي والزكاة المستحقة للمنشأة عن سنة مالية (طريقة صافي الأصول المبسطة).

تقدير يساعد المحاسب في إعداد الإقرار؛ الإقرار النهائي يُعد وفق لائحة جباية الزكاة الصادرة عن هيئة الزكاة والضريبة والجمارك.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.shortcuts import redirect, render

from accounts.models import Account, AccountType, GLEntry, RootType, ZakatTreatment
from core.models import Company, FiscalYear
from core.utils import flt

CLOSING_VOUCHER = "period-closing"
HIJRI_YEAR_DAYS = Decimal(354)
ZAKAT_EXPENSE = "مصروف الزكاة"
ZAKAT_PAYABLE = "مخصص الزكاة المستحقة"


def _group_ids(account):
    """الحساب الأب للحساب المعطى وكل ما تحته (لتمييز المتداول عن غير المتداول)."""
    return set(account.parent.descendant_ids()) if account and account.parent_id else set()


def treatment(account, current_assets, current_liabilities):
    if account.zakat_treatment:
        return account.zakat_treatment
    if account.root_type == RootType.EQUITY:
        return ZakatTreatment.ADD
    if account.root_type == RootType.LIABILITY:
        return ZakatTreatment.IGNORE if account.pk in current_liabilities else ZakatTreatment.ADD
    if account.root_type == RootType.ASSET:
        if account.account_type in (AccountType.FIXED_ASSET, AccountType.ACCUMULATED_DEPRECIATION):
            return ZakatTreatment.DEDUCT
        return ZakatTreatment.IGNORE if account.pk in current_assets else ZakatTreatment.DEDUCT
    return ZakatTreatment.IGNORE


def zakat_estimate(company, year, saudi_share=Decimal(100)):
    gl = GLEntry.all_objects.filter(company=company, is_cancelled=False).exclude(voucher_type=CLOSING_VOUCHER)
    balances = {row["account"]: flt(row["d"]) - flt(row["c"]) for row in
                gl.filter(posting_date__lte=year.end_date).values("account").annotate(d=Sum("debit"), c=Sum("credit"))}
    accounts = Account.all_objects.in_bulk(list(balances))
    current_assets = _group_ids(company.default_receivable_account)
    current_liabilities = _group_ids(company.default_payable_account)

    profit, zakat_expense = flt(0), flt(0)
    year_rows = gl.filter(posting_date__gte=year.start_date, posting_date__lte=year.end_date,
                          account__root_type__in=[RootType.INCOME, RootType.EXPENSE])
    for row in year_rows.values("account__name").annotate(d=Sum("debit"), c=Sum("credit")):
        change = flt(row["d"]) - flt(row["c"])
        profit -= change
        if row["account__name"] == ZAKAT_EXPENSE:
            zakat_expense += change
    adjusted_profit = profit + zakat_expense  # الزكاة نفسها لا تُحسم من الربح الخاضع

    additions, deductions = [], []
    for account_id, balance in balances.items():
        account = accounts[account_id]
        if not balance or account.root_type in (RootType.INCOME, RootType.EXPENSE):
            continue
        if account.name == ZAKAT_PAYABLE:
            continue
        kind = treatment(account, current_assets, current_liabilities)
        if kind == ZakatTreatment.ADD:
            additions.append({"account": account, "amount": -balance})       # رصيد دائن موجب
        elif kind == ZakatTreatment.DEDUCT:
            deductions.append({"account": account, "amount": balance})        # رصيد مدين موجب
    key = lambda r: (r["account"].number, r["account"].name)  # noqa: E731
    additions.sort(key=key)
    deductions.sort(key=key)
    total_additions = sum((r["amount"] for r in additions), flt(0)) + adjusted_profit
    total_deductions = sum((r["amount"] for r in deductions), flt(0))
    net_assets = total_additions - total_deductions
    base = max(net_assets, adjusted_profit, flt(0))
    days = Decimal((year.end_date - year.start_date).days + 1)
    rate = (Decimal("2.5") * days / HIJRI_YEAR_DAYS).quantize(Decimal("0.0001"))
    share = min(max(Decimal(saudi_share), Decimal(0)), Decimal(100))
    zakatable = flt(base * share / 100)
    return {
        "year": year, "additions": additions, "deductions": deductions, "profit": profit,
        "zakat_expense": zakat_expense, "adjusted_profit": adjusted_profit, "total_additions": total_additions,
        "total_deductions": total_deductions, "net_assets": net_assets, "base": base,
        "base_is_profit": base == adjusted_profit and adjusted_profit > net_assets, "days": days, "rate": rate,
        "share": share, "zakatable": zakatable, "zakat": flt(zakatable * rate / 100),
    }


def _zakat_account(company, name, sibling_field):
    sibling = company.require_account(sibling_field)
    account = Account.all_objects.filter(company=company, name=name, is_group=False).first()
    if account is None:
        account = Account.all_objects.create(company=company, name=name, parent=sibling.parent,
                                             root_type=sibling.root_type,
                                             account_type=AccountType.EXPENSE if sibling.root_type == RootType.EXPENSE else "",
                                             zakat_treatment=ZakatTreatment.IGNORE)
    return account


def _share(request):
    try:
        return Decimal((request.GET.get("share") or request.POST.get("share") or "100").replace(",", "."))
    except Exception:
        return Decimal(100)


@login_required
def zakat_report(request):
    years = FiscalYear.objects.all()
    year = years.filter(pk=request.GET.get("year") or request.POST.get("year") or 0).first() or years.first()
    company = Company.current()
    if request.method == "POST" and year:
        from accounts.models import JournalEntry
        from core.permissions import require_submit

        try:
            require_submit(request)
            estimate = zakat_estimate(company, year, _share(request))
            if estimate["zakat"] <= 0:
                raise ValidationError("لا توجد زكاة مستحقة لترحيل مخصصها.")
            amount = estimate["zakat"] - estimate["zakat_expense"]  # ما سبق ترحيله في السنة يُخصم
            if amount <= 0:
                raise ValidationError("مخصص الزكاة لهذه السنة مرحّل بالفعل.")
            je = JournalEntry.objects.create(posting_date=year.end_date, entry_type="adjustment",
                                             remarks=f"مخصص الزكاة التقديري لسنة {year}")
            je.accounts.create(account=_zakat_account(company, ZAKAT_EXPENSE, "default_expense_account"), debit=amount,
                               description=f"زكاة سنة {year}")
            je.accounts.create(account=_zakat_account(company, ZAKAT_PAYABLE, "default_payable_account"), credit=amount,
                               description=f"زكاة سنة {year}")
            je.calculate_totals()
            je.save()
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(f"{request.path}?year={year.pk}")
        messages.success(request, "أُنشئ قيد مخصص الزكاة كمسودة؛ راجعه ثم اعتمده.")
        return redirect(je.get_absolute_url())
    context = {"title": "الوعاء الزكوي والزكاة المستحقة", "years": years, "share": _share(request)}
    if year:
        context.update(zakat_estimate(company, year, context["share"]))
    return render(request, "reports/zakat.html", context)
