"""ترحيل القيود إلى دفتر الأستاذ العام."""
from django.core.exceptions import ValidationError

from core.utils import flt


def make_gl_entries(doc, entries, remarks=""):
    """إنشاء قيود الأستاذ لمستند معتمد بعد التحقق من التوازن.

    كل عنصر في entries قاموس يحتوي: account, debit, credit, customer, supplier, remarks
    """
    from accounts.models import GLEntry

    merged = {}
    for entry in entries:
        account = entry.get("account")
        if account is None:
            raise ValidationError("لم يتم تحديد حساب لأحد أطراف القيد.")
        if account.is_group:
            raise ValidationError(f"لا يمكن الترحيل على الحساب الرئيسي {account}.")
        customer, supplier = entry.get("customer"), entry.get("supplier")
        key = (account.id, getattr(customer, "id", None), getattr(supplier, "id", None))
        row = merged.setdefault(key, {
            "account": account, "customer": customer, "supplier": supplier,
            "debit": flt(0), "credit": flt(0), "remarks": entry.get("remarks") or remarks or doc.remarks,
        })
        row["debit"] += flt(entry.get("debit"))
        row["credit"] += flt(entry.get("credit"))

    rows = []
    for row in merged.values():
        net = row["debit"] - row["credit"]
        row["debit"], row["credit"] = (net, flt(0)) if net > 0 else (flt(0), -net)
        if row["debit"] or row["credit"]:
            rows.append(row)

    total_debit = sum((r["debit"] for r in rows), flt(0))
    total_credit = sum((r["credit"] for r in rows), flt(0))
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
            voucher_type=doc.doctype_slug,
            voucher_no=doc.name,
            voucher_id=doc.pk,
            remarks=(r["remarks"] or "")[:255],
        )
        for r in rows
    ])


def cancel_gl_entries(doc):
    from accounts.models import GLEntry

    GLEntry.objects.filter(voucher_type=doc.doctype_slug, voucher_no=doc.name).update(is_cancelled=True)
