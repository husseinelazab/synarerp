"""ترحيل القيود إلى دفتر الأستاذ العام."""
from django.core.exceptions import ValidationError

from core.utils import flt


ROUNDING_TOLERANCE = flt("0.05")


def make_gl_entries(doc, entries, remarks="", exchange_rate=None):
    """إنشاء قيود الأستاذ لمستند معتمد بعد التحقق من التوازن.

    كل عنصر في entries قاموس يحتوي: account, debit, credit, customer, supplier, cost_center, remarks.
    إذا مُرر exchange_rate تُحوّل المبالغ من عملة المستند إلى عملة الشركة، إلا العناصر المعلّمة base=True
    (مثل تكلفة المخزون المحسوبة أصلاً بعملة الشركة). فرق التقريب الناتج عن التحويل يُحمّل على سطر الطرف.

    الحساب بعملة أجنبية (بنك بالدولار مثلاً) يحتاج مبلغه بعملته: account_amount (موجب للمدين)، أو يُستنتج
    إن كان المستند بنفس عملة الحساب. غير ذلك يُرفض الترحيل حتى لا يختل رصيد الحساب بعملته.
    """
    from accounts.models import GLEntry, RootType

    default_center = getattr(doc, "cost_center", None) or doc.company.default_cost_center
    merged = {}
    for entry in entries:
        account = entry.get("account")
        if account is None:
            raise ValidationError("لم يتم تحديد حساب لأحد أطراف القيد.")
        if account.is_group:
            raise ValidationError(f"لا يمكن الترحيل على الحساب الرئيسي {account}.")
        customer, supplier = entry.get("customer"), entry.get("supplier")
        # مركز التكلفة يُسجل على حسابات قائمة الدخل فقط (الإيرادات والمصروفات)
        center = None
        if account.root_type in (RootType.INCOME, RootType.EXPENSE):
            center = entry.get("cost_center") or default_center
            if center is not None and center.is_group:
                raise ValidationError(f"لا يمكن الترحيل على مركز التكلفة الرئيسي {center}.")
        key = (account.id, getattr(customer, "id", None), getattr(supplier, "id", None), getattr(center, "id", None))
        row = merged.setdefault(key, {
            "account": account, "customer": customer, "supplier": supplier, "cost_center": center,
            "debit": flt(0), "credit": flt(0), "remarks": entry.get("remarks") or remarks or doc.remarks,
            "account_amount": flt(0),
        })
        rate = exchange_rate if exchange_rate and not entry.get("base") else 1
        row["debit"] += flt(flt(entry.get("debit")) * rate)
        row["credit"] += flt(flt(entry.get("credit")) * rate)
        if _is_foreign(doc, account):
            row["account_amount"] += _account_amount(doc, account, entry)

    rows = []
    for row in merged.values():
        net = row["debit"] - row["credit"]
        row["debit"], row["credit"] = (net, flt(0)) if net > 0 else (flt(0), -net)
        if row["debit"] or row["credit"] or row["account_amount"]:
            rows.append(row)

    total_debit = sum((r["debit"] for r in rows), flt(0))
    total_credit = sum((r["credit"] for r in rows), flt(0))
    difference = total_debit - total_credit
    if difference and exchange_rate and exchange_rate != 1 and abs(difference) <= ROUNDING_TOLERANCE:
        # فرق تقريب التحويل يُسوّى على سطر العميل/المورد (أو أكبر سطر)
        target = next((r for r in rows if r["customer"] or r["supplier"]), max(rows, key=lambda r: r["debit"] + r["credit"]))
        net = target["debit"] - target["credit"] - difference
        target["debit"], target["credit"] = (net, flt(0)) if net > 0 else (flt(0), -net)
        total_debit = total_credit = sum((r["debit"] for r in rows), flt(0))
    if total_debit != total_credit:
        raise ValidationError(f"القيد غير متوازن: المدين {total_debit} والدائن {total_credit}.")

    GLEntry.objects.bulk_create([
        GLEntry(
            company=doc.company,
            posting_date=doc.posting_date,
            account=r["account"],
            debit=r["debit"],
            credit=r["credit"],
            customer=r["customer"],
            supplier=r["supplier"],
            cost_center=r["cost_center"],
            voucher_type=doc.doctype_slug,
            voucher_no=doc.name,
            voucher_id=doc.pk,
            remarks=(r["remarks"] or "")[:255],
            **_account_currency_fields(doc, r),
        )
        for r in rows
    ])


def _is_foreign(doc, account):
    return bool(account.account_currency_id) and account.account_currency_id != doc.company.currency_id


def _account_amount(doc, account, entry):
    if entry.get("account_amount") is not None:
        return flt(entry["account_amount"])
    if getattr(doc, "currency_id", None) == account.account_currency_id and not entry.get("base"):
        return flt(entry.get("debit")) - flt(entry.get("credit"))
    raise ValidationError(f"الحساب {account} بعملة {account.account_currency.code}؛ "
                          f"لا يمكن الترحيل عليه من هذا المستند. استخدم سنداً أو قيداً بعملة الحساب.")


def _account_currency_fields(doc, row):
    account = row["account"]
    if not _is_foreign(doc, account):
        return {}
    amount = row["account_amount"]
    return {"account_currency_id": account.account_currency_id,
            "debit_in_account_currency": amount if amount > 0 else flt(0),
            "credit_in_account_currency": -amount if amount < 0 else flt(0)}


def cancel_gl_entries(doc):
    from accounts.models import GLEntry

    from accounts.models import BankTransaction

    entries = GLEntry.all_objects.filter(voucher_type=doc.doctype_slug, voucher_id=doc.pk)
    entries.update(is_cancelled=True, clearance_date=None)
    # القيد الملغى لم يعد مطابقاً لأي حركة في كشف البنك
    BankTransaction.all_objects.filter(matched_entry__in=entries).update(matched_entry=None)
