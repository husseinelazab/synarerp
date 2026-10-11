"""التسوية البنكية: مطابقة قيود حساب البنك في الدفاتر مع حركات كشف البنك."""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.models import Account, AccountType, BankTransaction, GLEntry
from core.utils import flt

MATCH_WINDOW_DAYS = 5


def _book_amount(gle):
    # الحساب بعملة أجنبية يُطابق بعملته، كما في كشف البنك
    return gle.account_amount


def match(gle, line):
    if gle.account_id != line.account_id:
        raise ValidationError("القيد والحركة على حسابين مختلفين.")
    if _book_amount(gle) != line.amount:
        raise ValidationError(f"المبلغان غير متساويين: الدفاتر {_book_amount(gle)} والكشف {line.amount}.")
    if gle.is_cancelled or line.matched_entry_id or gle.clearance_date:
        raise ValidationError("القيد أو الحركة مطابق مسبقاً.")
    line.matched_entry = gle
    line.save(update_fields=["matched_entry"])
    gle.clearance_date = line.date
    gle.save(update_fields=["clearance_date"])


def unmatch(line):
    if line.matched_entry_id:
        GLEntry.objects.filter(pk=line.matched_entry_id).update(clearance_date=None)
        line.matched_entry = None
        line.save(update_fields=["matched_entry"])


def auto_match(account, to_date):
    """مطابقة تلقائية واحد لواحد: نفس المبلغ والاتجاه وتاريخ متقارب، مع تفضيل تطابق المرجع ثم أقرب تاريخ."""
    lines = list(BankTransaction.objects.filter(account=account, matched_entry__isnull=True, date__lte=to_date))
    entries = list(GLEntry.objects.filter(account=account, is_cancelled=False, clearance_date__isnull=True,
                                          posting_date__lte=to_date + datetime.timedelta(days=MATCH_WINDOW_DAYS)))
    used, count = set(), 0
    for line in lines:
        window = datetime.timedelta(days=MATCH_WINDOW_DAYS)
        candidates = [g for g in entries if g.pk not in used and _book_amount(g) == line.amount
                      and abs(g.posting_date - line.date) <= window]
        if not candidates:
            continue
        candidates.sort(key=lambda g: (not (line.reference and line.reference in (g.voucher_no + g.remarks)),
                                       abs(g.posting_date - line.date)))
        match(candidates[0], line)
        used.add(candidates[0].pk)
        count += 1
    return count


def reconciliation_summary(account, to_date, statement_balance=None):
    active = GLEntry.objects.filter(account=account, is_cancelled=False, posting_date__lte=to_date)
    debit, credit = ("debit_in_account_currency", "credit_in_account_currency") if account.account_currency_id \
        else ("debit", "credit")
    agg = active.aggregate(d=Sum(debit), c=Sum(credit))
    book_balance = flt(agg["d"]) - flt(agg["c"])
    uncleared = active.filter(Q(clearance_date__isnull=True) | Q(clearance_date__gt=to_date))
    in_transit = flt(uncleared.aggregate(v=Sum(debit))["v"])       # إيداعات لم تظهر في الكشف بعد
    outstanding = flt(uncleared.aggregate(v=Sum(credit))["v"])    # مدفوعات/شيكات لم تُصرف بعد
    expected = book_balance - in_transit + outstanding
    summary = {"book_balance": book_balance, "in_transit": in_transit, "outstanding": outstanding,
               "expected": expected, "statement_balance": statement_balance}
    if statement_balance is not None:
        summary["difference"] = flt(statement_balance) - expected
    return summary


@login_required
def bank_reconciliation(request):
    accounts = Account.objects.filter(is_group=False, account_type__in=[AccountType.BANK, AccountType.CASH])
    account = accounts.filter(pk=request.GET.get("account") or request.POST.get("account") or 0).first() \
        or accounts.filter(account_type=AccountType.BANK).first()
    to_date = parse_date(request.GET.get("to_date") or request.POST.get("to_date") or "") or timezone.localdate()
    raw_balance = (request.GET.get("statement_balance") or "").replace(",", "")
    try:
        statement_balance = flt(raw_balance) if raw_balance else None
    except Exception:
        statement_balance = None

    if request.method == "POST" and account:
        action = request.POST.get("action")
        try:
            with transaction.atomic():
                if action == "auto":
                    count = auto_match(account, to_date)
                    messages.success(request, f"تمت مطابقة {count} حركة تلقائياً.")
                elif action == "match":
                    gle = GLEntry.objects.get(pk=request.POST.get("entry"), account=account)
                    line = BankTransaction.objects.get(pk=request.POST.get("line"), account=account)
                    match(gle, line)
                    messages.success(request, "تمت المطابقة.")
                elif action == "unmatch":
                    unmatch(BankTransaction.objects.get(pk=request.POST.get("line"), account=account))
                    messages.success(request, "تم إلغاء المطابقة.")
        except (ValidationError, GLEntry.DoesNotExist, BankTransaction.DoesNotExist) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", ["اختر قيداً وحركة صحيحين."])))
        query = f"?account={account.pk}&to_date={to_date:%Y-%m-%d}"
        if raw_balance:
            query += f"&statement_balance={raw_balance}"
        return redirect(request.path + query)

    context = {"accounts": accounts, "account": account, "to_date": to_date, "statement_balance": raw_balance}
    if account:
        book_entries = GLEntry.objects.filter(account=account, is_cancelled=False, clearance_date__isnull=True,
                                              posting_date__lte=to_date).order_by("posting_date", "id")
        if account.account_currency_id:
            # قيود إعادة التقييم تغيّر القيمة بعملة الشركة فقط ولا تظهر في كشف البنك
            book_entries = book_entries.exclude(debit_in_account_currency=0, credit_in_account_currency=0)
        context.update({
            "summary": reconciliation_summary(account, to_date, statement_balance),
            "book_entries": book_entries,
            "statement_lines": BankTransaction.objects.filter(account=account, matched_entry__isnull=True,
                                                              date__lte=to_date),
            "matched": BankTransaction.objects.filter(account=account, matched_entry__isnull=False,
                                                      date__lte=to_date).select_related("matched_entry").order_by("-date")[:50],
        })
    return render(request, "reports/bank_reconciliation.html", context)
