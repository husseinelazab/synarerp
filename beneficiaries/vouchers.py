"""القسائم الشرائية للمستفيدين: تصدر بصرف مساعدة من نوع «قسيمة»، ويستخدمها المستفيد لدى متجر شريك.

القيود:
- الإصدار:   من ح/ مصروف البرنامج (والصندوق)        إلى ح/ القسائم الشرائية المستحقة (التزام)
- الاستخدام: من ح/ القسائم الشرائية المستحقة          إلى ح/ الموردين (المتجر)، فيُسدَّد له بسند صرف عادي
- الانتهاء:  من ح/ القسائم الشرائية المستحقة          إلى ح/ مصروف البرنامج (يعود الرصيد غير المستخدم للصندوق)

يتحقق المتجر من المستفيد برقم القسيمة وآخر 4 أرقام من هويته؛ بعد 5 محاولات خاطئة تُقفل القسيمة.
"""
import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.utils import flt

LIABILITY_NAME = "القسائم الشرائية المستحقة للمتاجر"


def liability_account(company):
    """حساب التزام القسائم: يُنشأ تلقائياً تحت مجموعة حساب الموردين عند أول قسيمة."""
    from accounts.models import Account, RootType

    account = Account.all_objects.filter(company=company, name=LIABILITY_NAME, is_group=False).first()
    if account:
        return account
    payable = company.require_account("default_payable_account")
    return Account.all_objects.create(company=company, name=LIABILITY_NAME, parent=payable.parent,
                                      root_type=RootType.LIABILITY, is_group=False)


def allowed_merchants(program):
    from beneficiaries.models import VoucherMerchant

    chosen = list(program.merchants.filter(is_active=True))
    return chosen or list(VoucherMerchant.all_objects.filter(company=program.company, is_active=True))


# ───────── الإصدار ─────────

def validate_issue(aid):
    if not aid.amount or aid.amount <= 0:
        raise ValidationError("أدخل قيمة القسيمة.")
    if not aid.beneficiary.phone:
        raise ValidationError("أضف جوال المستفيد لتصله القسيمة برسالة.")
    if len((aid.beneficiary.national_id or "").strip()) < 4:
        raise ValidationError("رقم هوية المستفيد مطلوب: يتحقق به المتجر عند استخدام القسيمة.")
    if not allowed_merchants(aid.program):
        raise ValidationError("لا يوجد متجر شريك مفعّل لهذا البرنامج. أضف متجراً من «المتاجر الشريكة».")


def issue_voucher(aid, remarks):
    from accounts.ledger import make_gl_entries
    from beneficiaries.models import AidVoucher

    make_gl_entries(aid, [{"account": aid.program.expense_account, "debit": aid.amount, "fund": aid.fund},
                          {"account": liability_account(aid.company), "credit": aid.amount}], remarks=remarks)
    voucher = AidVoucher.all_objects.create(
        company=aid.company, disbursement=aid, beneficiary=aid.beneficiary, code=AidVoucher.new_code(),
        amount=aid.amount, balance=aid.amount,
        expires_on=aid.posting_date + datetime.timedelta(days=aid.program.voucher_valid_days or 30))
    send_voucher_message(voucher)
    return voucher


def voucher_message(voucher):
    stores = "، ".join(str(m) for m in allowed_merchants(voucher.disbursement.program)[:3])
    return (f"صدرت لكم قسيمة شرائية من {voucher.company.name} بقيمة {flt(voucher.amount):,.2f} ريال، "
            f"رقمها {voucher.code}، صالحة حتى {voucher.expires_on:%Y-%m-%d}"
            + (f" لدى: {stores}" if stores else "") + ". أبرز هويتك عند الشراء ولا تشارك رقم القسيمة.")


def send_voucher_message(voucher):
    """يرسل رقم القسيمة للمستفيد عبر مزود الرسائل إن كان مفعّلاً. يعيد True عند الإرسال."""
    from messaging.models import MessagingSettings
    from messaging.service import send

    cfg = MessagingSettings.all_objects.filter(company=voucher.company).first()
    if cfg is None or not cfg.active:
        return False
    try:
        send(voucher.company, voucher.beneficiary.phone, voucher_message(voucher), event="aid_voucher",
             reference=voucher.code, mode="provider")
    except ValueError:
        return False
    return True


def void_voucher(aid):
    from beneficiaries.models import VoucherStatus

    voucher = getattr(aid, "voucher", None)
    if voucher is None:
        return
    if voucher.redemptions.filter(docstatus=1, kind="redeem").exists():
        raise ValidationError("استُخدمت القسيمة لدى متجر؛ ألغِ عمليات استخدامها أولاً.")
    voucher.status, voucher.balance = VoucherStatus.VOID, flt(0)
    voucher.save(update_fields=["status", "balance"])


# ───────── الاستخدام والانتهاء ─────────

def _refresh_status(voucher):
    from beneficiaries.models import VoucherStatus

    if voucher.status == VoucherStatus.VOID:
        return
    if voucher.balance <= 0:
        voucher.status = VoucherStatus.EXPIRED if voucher.redemptions.filter(
            docstatus=1, kind="expire").exists() else VoucherStatus.USED
    else:
        voucher.status = VoucherStatus.EXPIRED if voucher.redemptions.filter(
            docstatus=1, kind="expire").exists() else VoucherStatus.ACTIVE


def validate_redemption(redemption):
    from beneficiaries.models import AidVoucher, VoucherStatus

    voucher = AidVoucher.all_objects.select_for_update().get(pk=redemption.voucher_id)
    if redemption.amount is None or redemption.amount <= 0:
        raise ValidationError("المبلغ يجب أن يكون أكبر من صفر.")
    if voucher.status != VoucherStatus.ACTIVE:
        raise ValidationError(f"القسيمة {voucher.get_status_display()}.")
    if redemption.amount > voucher.balance:
        raise ValidationError(f"المبلغ أكبر من رصيد القسيمة ({flt(voucher.balance):,.2f}).")
    if redemption.kind == "redeem":
        if redemption.merchant_id is None:
            raise ValidationError("حدد المتجر.")
        if not redemption.merchant.is_active:
            raise ValidationError("المتجر موقوف.")
        if redemption.merchant not in allowed_merchants(voucher.disbursement.program):
            raise ValidationError("هذا المتجر غير معتمد لقسائم هذا البرنامج.")
        if voucher.expires_on < redemption.posting_date:
            raise ValidationError(f"انتهت صلاحية القسيمة في {voucher.expires_on:%Y-%m-%d}.")


def post_redemption(redemption):
    from accounts.ledger import make_gl_entries
    from beneficiaries.models import AidVoucher

    voucher = AidVoucher.all_objects.select_for_update().get(pk=redemption.voucher_id)
    aid = voucher.disbursement
    liability = liability_account(redemption.company)
    if redemption.kind == "redeem":
        supplier = redemption.merchant.supplier
        entries = [{"account": liability, "debit": redemption.amount},
                   {"account": redemption.company.require_account("default_payable_account"),
                    "credit": redemption.amount, "supplier": supplier}]
        remarks = f"قسيمة {voucher.code} لدى {redemption.merchant}" + (
            f" — فاتورة {redemption.invoice_no}" if redemption.invoice_no else "")
    else:
        entries = [{"account": liability, "debit": redemption.amount},
                   {"account": aid.program.expense_account, "credit": redemption.amount, "fund": aid.fund}]
        remarks = f"إرجاع رصيد القسيمة المنتهية {voucher.code}"
    make_gl_entries(redemption, entries, remarks=remarks)
    voucher.balance = flt(voucher.balance - redemption.amount)
    _refresh_status(voucher)
    voucher.save(update_fields=["balance", "status"])


def reverse_redemption(redemption):
    from accounts.ledger import cancel_gl_entries
    from beneficiaries.models import AidVoucher

    cancel_gl_entries(redemption)
    voucher = AidVoucher.all_objects.select_for_update().get(pk=redemption.voucher_id)
    voucher.balance = flt(voucher.balance + redemption.amount)
    _refresh_status(voucher)
    voucher.save(update_fields=["balance", "status"])


def find_for_merchant(merchant, code, id_last4):
    """القسيمة برقمها بعد التحقق من آخر 4 أرقام هوية المستفيد. الخطأ يزيد عداد المحاولات ويقفلها بعد 5."""
    from beneficiaries.models import AidVoucher

    code = "".join(ch for ch in (code or "") if ch.isdigit())
    voucher = (AidVoucher.all_objects.select_related("beneficiary", "disbursement__program")
               .filter(company=merchant.company, code=code).first())
    if voucher is None:
        raise ValidationError("رقم القسيمة غير صحيح.")
    if voucher.is_locked:
        raise ValidationError("القسيمة مقفلة لكثرة المحاولات الخاطئة. تواصل مع الجمعية.")
    national_id = (voucher.beneficiary.national_id or "").strip()
    if not id_last4 or national_id[-4:] != (id_last4 or "").strip():
        voucher.failed_attempts += 1
        voucher.save(update_fields=["failed_attempts"])
        raise ValidationError("آخر 4 أرقام من الهوية غير مطابقة.")
    if voucher.failed_attempts:
        voucher.failed_attempts = 0
        voucher.save(update_fields=["failed_attempts"])
    return voucher


def redeem(merchant, voucher, amount, invoice_no=""):
    from django.db import transaction

    from beneficiaries.models import VoucherRedemption

    with transaction.atomic():
        redemption = VoucherRedemption(company=merchant.company, voucher=voucher, kind="redeem", merchant=merchant,
                                       amount=flt(amount), invoice_no=(invoice_no or "")[:60],
                                       posting_date=timezone.localdate(),
                                       remarks=f"استخدام عبر صفحة المتجر {merchant}")
        redemption.save()
        redemption.submit()
    return redemption


def expire_due(company, today=None):
    """يرجع أرصدة القسائم المنتهية للبرنامج والصندوق. يعيد عدد القسائم."""
    from django.db import transaction

    from beneficiaries.models import AidVoucher, VoucherRedemption, VoucherStatus

    today = today or timezone.localdate()
    count = 0
    for voucher in AidVoucher.all_objects.filter(company=company, status=VoucherStatus.ACTIVE, expires_on__lt=today,
                                                 balance__gt=0):
        with transaction.atomic():
            redemption = VoucherRedemption(company=company, voucher=voucher, kind="expire", amount=voucher.balance,
                                           posting_date=today, remarks="انتهاء صلاحية القسيمة")
            redemption.save()
            redemption.submit()
        count += 1
    return count
