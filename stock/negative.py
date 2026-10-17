"""Deficit allocations preserve provisional issue costs and settle COGS on receipt date."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import F, Q, Sum

from core.utils import flt


def permits_negative(doc, item, warehouse):
    from pos.models import POSProfile
    return (getattr(doc, "doctype_slug", "") == "sales-invoice" and getattr(doc, "is_pos", False)
            and not doc.is_return and doc.pos_shift_id and not item.has_batch_no and not item.has_serial_no
            and POSProfile.objects.filter(pk=doc.pos_shift.profile_id, company_id=doc.company_id,
                warehouse=warehouse, allow_negative_stock=True, disabled=False).exists())


def settle_receipt(doc, receipt, book_value):
    from accounts.ledger import make_gl_entries
    from selling.models import SalesInvoice
    from stock.models import NegativeStockSettlement, StockLedgerEntry

    remaining = receipt.actual_qty
    issues = StockLedgerEntry.objects.select_for_update().filter(company=doc.company, item=receipt.item,
        warehouse=receipt.warehouse, is_cancelled=False, negative_remaining_qty__gt=0).order_by("id")
    # Returns first restore the deficit of their original sale, then the oldest other deficit.
    rows = list(issues)
    if getattr(doc, "return_against_id", None) and getattr(doc, "is_return", False):
        rows.sort(key=lambda i: (i.voucher_type != doc.doctype_slug or i.voucher_id != doc.return_against_id, i.pk))
    correction = Decimal(0)
    for issue in rows:
        if remaining <= 0:
            break
        part = min(remaining, issue.negative_remaining_qty)
        provisional = (issue.negative_remaining_value if part == issue.negative_remaining_qty else
                       flt(issue.negative_remaining_value * part / issue.negative_remaining_qty))
        actual = flt(part * receipt.incoming_rate)
        delta = flt(actual - provisional)
        issue.negative_remaining_qty -= part
        issue.negative_remaining_value -= provisional
        issue.save(update_fields=["negative_remaining_qty", "negative_remaining_value"])
        adjustment = None
        if delta:
            adjustment = StockLedgerEntry.objects.create(company=doc.company, posting_date=doc.posting_date,
                item=receipt.item, warehouse=receipt.warehouse, actual_qty=0, incoming_rate=0,
                valuation_rate=receipt.valuation_rate, qty_after_transaction=receipt.qty_after_transaction,
                stock_value_difference=-delta, voucher_type=doc.doctype_slug, voucher_no=doc.name, voucher_id=doc.pk)
            source = SalesInvoice.objects.get(pk=issue.voucher_id, company=doc.company)
            from types import SimpleNamespace
            correction_doc = SimpleNamespace(company=doc.company, posting_date=doc.posting_date,
                doctype_slug=doc.doctype_slug, name=doc.name, pk=doc.pk, remarks=doc.remarks,
                cost_center=source.cost_center, project=source.project, fund=source.fund)
            make_gl_entries(correction_doc, [
                {"account": doc.company.require_account("default_cogs_account"), "debit": delta,
                 "cost_center": source.cost_center, "project": source.project, "fund": source.fund},
                {"account": doc.company.require_account("default_stock_account"), "credit": delta},
            ], remarks=f"تسوية تكلفة البيع بالسالب {source.name} - {receipt.item}")
        NegativeStockSettlement.objects.create(company=doc.company, issue=issue, receipt=receipt,
            adjustment=adjustment, quantity=part, provisional_value=provisional, actual_value=actual, difference=delta)
        correction += delta
        remaining -= part
    return flt(book_value - correction)


def prepare_cancellation(doc, entries):
    """Reverse allocations only in reverse stock-movement order; never silently rewrite later costs."""
    from stock.models import NegativeStockSettlement, StockLedgerEntry

    ids = [e.pk for e in entries]
    for entry in entries:
        if entry.actual_qty < 0 and StockLedgerEntry.objects.filter(item=entry.item, warehouse=entry.warehouse,
                is_cancelled=False, negative_remaining_qty__gt=0, pk__gt=entry.pk).exclude(pk__in=ids).exists():
            raise ValidationError("توجد مبيعات سالبة لاحقة. ألغِ الحركات اللاحقة أولاً قبل عكس هذا الصرف.")
    if NegativeStockSettlement.objects.filter(issue_id__in=ids, is_cancelled=False).exists():
        raise ValidationError("سُوِّيت تكلفة هذا البيع بالسالب. ألغِ مستندات التغطية المرتبطة أولاً.")
    allocations = list(NegativeStockSettlement.objects.filter(receipt_id__in=ids, is_cancelled=False)
                       .select_related("issue", "receipt"))
    for allocation in allocations:
        if StockLedgerEntry.objects.filter(item=allocation.receipt.item, warehouse=allocation.receipt.warehouse,
                is_cancelled=False, pk__gt=max(ids)).exists():
            raise ValidationError("توجد حركات لاحقة لتسوية النقص. ألغِ الحركات اللاحقة أولاً لإعادة التكلفة بأمان.")
    for allocation in allocations:
        StockLedgerEntry.objects.filter(pk=allocation.issue_id).update(
            negative_remaining_qty=F("negative_remaining_qty") + allocation.quantity,
            negative_remaining_value=F("negative_remaining_value") + allocation.provisional_value)
        allocation.is_cancelled = True
        allocation.save(update_fields=["is_cancelled"])
    return bool(allocations)


def cost_corrections(voucher_type, ids):
    from stock.models import NegativeStockSettlement
    rows = NegativeStockSettlement.objects.filter(is_cancelled=False, issue__is_cancelled=False,
        issue__voucher_type=voucher_type, issue__voucher_id__in=ids).values("issue__voucher_id", "issue__item_id").annotate(value=Sum("difference"))
    return {(r["issue__voucher_id"], r["issue__item_id"]): r["value"] for r in rows}
