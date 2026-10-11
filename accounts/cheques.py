"""الشيكات الواردة والصادرة: الحساب الوسيط، الصرف من البنك، الارتداد، وتقرير الاستحقاقات."""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from core.utils import flt

# (حقل الشركة، الاسم، الرقم المقترح، حقل الحساب الشقيق الذي يُنشأ بجانبه)
CHEQUE_ACCOUNTS = {
    True: ("default_cheques_receivable_account", "شيكات تحت التحصيل", "1108", "default_receivable_account"),
    False: ("default_cheques_payable_account", "شيكات مستحقة الدفع", "2107", "default_payable_account"),
}


def cheques_account(company, received):
    """حساب الشيكات الواردة (أصل) أو الصادرة (خصم)؛ يُنشأ تلقائياً للشركات القائمة بجانب حساب العملاء/الموردين."""
    from accounts.models import Account

    field, name, number, sibling_field = CHEQUE_ACCOUNTS[received]
    account = getattr(company, field)
    if account is not None:
        return account
    sibling = company.require_account(sibling_field)
    account = Account.all_objects.filter(company=company, name=name, is_group=False).first()
    if account is None:
        taken = Account.all_objects.filter(company=company, number=number).exists()
        account = Account.all_objects.create(company=company, name=name, number="" if taken else number,
                                             parent=sibling.parent, root_type=sibling.root_type)
    setattr(company, field, account)
    company.save(update_fields=[field])
    return account


def _journal(cheque, date, lines, remarks):
    from accounts.models import JournalEntry

    je = JournalEntry.objects.create(company=cheque.company, posting_date=date, remarks=remarks)
    for line in lines:
        je.accounts.create(**line)
    je.calculate_totals()
    je.submit()
    return je


def clear_cheque(cheque, date):
    """صرف الشيك: ينتقل المبلغ من حساب الشيكات إلى البنك (أو من البنك للمستفيد في الصادر)."""
    from accounts.models import ChequeStatus

    if cheque.status != ChequeStatus.PENDING:
        raise ValidationError("يمكن صرف الشيكات المعلقة فقط.")
    payment = cheque.payment
    if date < payment.posting_date:
        raise ValidationError("تاريخ الصرف قبل تاريخ السند.")
    holding = cheques_account(cheque.company, cheque.is_received)
    amount = payment.base_amount
    label = f"صرف الشيك {payment.cheque_no} — {payment.party} ({payment.name})"
    if cheque.is_received:
        lines = [{"account": payment.account, "debit": amount, "description": label},
                 {"account": holding, "credit": amount, "description": label}]
    else:
        lines = [{"account": holding, "debit": amount, "description": label},
                 {"account": payment.account, "credit": amount, "description": label}]
    cheque.clearance_entry = _journal(cheque, date, lines, label)
    cheque.status, cheque.status_date = ChequeStatus.CLEARED, date
    cheque.save(update_fields=["clearance_entry", "status", "status_date"])


def bounce_cheque(cheque, date, reason=""):
    """ارتداد الشيك: يعود المبلغ ديناً على العميل (أو للمورد في الصادر) وتُفتح الفاتورة من جديد."""
    from accounts.models import ChequeStatus

    if cheque.status != ChequeStatus.PENDING:
        raise ValidationError("يمكن تسجيل ارتداد الشيكات المعلقة فقط.")
    payment = cheque.payment
    if date < payment.posting_date:
        raise ValidationError("تاريخ الارتداد قبل تاريخ السند.")
    holding = cheques_account(cheque.company, cheque.is_received)
    amount = payment.base_amount
    label = f"ارتداد الشيك {payment.cheque_no} — {payment.party} ({payment.name})" + (f": {reason}" if reason else "")
    if payment.customer_id:
        party_line = {"account": cheque.company.require_account("default_receivable_account"), "customer": payment.customer}
    else:
        party_line = {"account": cheque.company.require_account("default_payable_account"), "supplier": payment.supplier}
    if cheque.is_received:
        lines = [{**party_line, "debit": amount, "description": label},
                 {"account": holding, "credit": amount, "description": label}]
    else:
        lines = [{"account": holding, "debit": amount, "description": label},
                 {**party_line, "credit": amount, "description": label}]
    cheque.bounce_entry = _journal(cheque, date, lines, label[:255])
    cheque.status, cheque.status_date, cheque.bounce_reason = ChequeStatus.BOUNCED, date, reason[:255]
    cheque.save(update_fields=["bounce_entry", "status", "status_date", "bounce_reason"])
    payment._update_invoice_outstanding(flt(payment.amount) * payment.direction)


def undo_cheque(cheque):
    """التراجع عن الصرف أو الارتداد بإلغاء قيده، فيعود الشيك معلقاً."""
    from accounts.models import ChequeStatus

    entry = cheque.clearance_entry or cheque.bounce_entry
    if cheque.status not in (ChequeStatus.CLEARED, ChequeStatus.BOUNCED) or entry is None:
        raise ValidationError("لا يوجد صرف أو ارتداد للتراجع عنه.")
    entry.cancel()  # يعيد الحالة عبر on_journal_cancel


def on_journal_cancel(journal):
    """إلغاء قيد صرف أو ارتداد (من هنا أو من صفحة القيد) يعيد الشيك معلقاً."""
    from accounts.models import Cheque, ChequeStatus

    for cheque in Cheque.all_objects.filter(company=journal.company, clearance_entry=journal):
        cheque.status, cheque.status_date, cheque.clearance_entry = ChequeStatus.PENDING, None, None
        cheque.save(update_fields=["status", "status_date", "clearance_entry"])
    for cheque in Cheque.all_objects.filter(company=journal.company, bounce_entry=journal).select_related("payment"):
        cheque.status, cheque.status_date, cheque.bounce_entry, cheque.bounce_reason = ChequeStatus.PENDING, None, None, ""
        cheque.save(update_fields=["status", "status_date", "bounce_entry", "bounce_reason"])
        payment = cheque.payment
        payment._update_invoice_outstanding(-flt(payment.amount) * payment.direction)


@login_required
def cheques_report(request):
    from accounts.models import Cheque, ChequeStatus, PaymentType

    today = timezone.localdate()
    direction = request.GET.get("direction", "")
    status = request.GET.get("status", ChequeStatus.PENDING)
    try:
        days = max(int(request.GET.get("days") or 0), 0)
    except ValueError:
        days = 0
    qs = Cheque.objects.select_related("payment__customer", "payment__supplier", "payment__account")
    if direction in (PaymentType.RECEIVE, PaymentType.PAY):
        qs = qs.filter(payment__payment_type=direction)
    if status in ChequeStatus.values:
        qs = qs.filter(status=status)
    if days:
        qs = qs.filter(payment__cheque_date__lte=today + datetime.timedelta(days=days))
    cheques = list(qs.order_by("payment__cheque_date", "id"))
    received = [c for c in cheques if c.is_received]
    issued = [c for c in cheques if not c.is_received]
    pending = Cheque.objects.filter(status=ChequeStatus.PENDING)
    return render(request, "reports/cheques.html", {
        "title": "الشيكات", "today": today, "direction": direction, "status": status, "days": days,
        "statuses": ChequeStatus.choices, "received": received, "issued": issued,
        "received_total": sum((c.payment.amount for c in received), flt(0)),
        "issued_total": sum((c.payment.amount for c in issued), flt(0)),
        "due_received": pending.filter(payment__payment_type=PaymentType.RECEIVE, payment__cheque_date__lte=today)
        .aggregate(v=Sum("payment__amount"))["v"] or 0,
        "due_issued": pending.filter(payment__payment_type=PaymentType.PAY, payment__cheque_date__lte=today)
        .aggregate(v=Sum("payment__amount"))["v"] or 0,
    })


@login_required
def cheque_action(request, pk):
    from accounts.models import Cheque
    from core.permissions import require_submit

    cheque = get_object_or_404(Cheque.objects.select_related("payment"), pk=pk)
    back = request.POST.get("next") or cheque.payment.get_absolute_url()
    if not back.startswith("/") or back.startswith("//"):
        back = cheque.payment.get_absolute_url()
    if request.method != "POST":
        return redirect(back)
    action = request.POST.get("action")
    date = parse_date(request.POST.get("date") or "") or timezone.localdate()
    try:
        require_submit(request)
        with transaction.atomic():
            if action == "clear":
                clear_cheque(cheque, date)
                messages.success(request, f"تم تسجيل صرف الشيك {cheque.cheque_no}.")
            elif action == "bounce":
                bounce_cheque(cheque, date, (request.POST.get("reason") or "").strip())
                messages.success(request, f"تم تسجيل ارتداد الشيك {cheque.cheque_no} وإعادة المبلغ على الطرف.")
            elif action == "undo":
                undo_cheque(cheque)
                messages.success(request, f"عاد الشيك {cheque.cheque_no} معلقاً.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(back)
