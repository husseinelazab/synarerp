"""شاشة تسوية الدفعات: ربط الدفعات المقدمة والإشعارات الدائنة بالفواتير المفتوحة لطرف واحد."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render

from accounts.models import PartyType, PaymentEntry, PaymentReconciliation, PaymentType
from core.utils import flt


def open_items(party_field, party):
    """(المصادر المتاحة للتسوية، الفواتير المفتوحة) للطرف، الأقدم أولاً."""
    from buying.models import PurchaseInvoice
    from selling.models import SalesInvoice

    invoice_model = SalesInvoice if party_field == "customer" else PurchaseInvoice
    normal = PaymentType.RECEIVE if party_field == "customer" else PaymentType.PAY
    sources = [{"key": f"p-{p.pk}", "doc": p, "available": flt(p.unallocated_amount), "currency": p.currency}
               for p in PaymentEntry.objects.filter(**{party_field: party}, docstatus=1, payment_type=normal,
                                                    unallocated_amount__gt=0).select_related("currency")
               .order_by("posting_date", "pk")]
    sources += [{"key": f"r-{r.pk}", "doc": r, "available": -flt(r.outstanding_amount), "currency": r.currency}
                for r in invoice_model.objects.filter(**{party_field: party}, docstatus=1, is_return=True,
                                                      outstanding_amount__lt=0).select_related("currency")
                .order_by("posting_date", "pk")]
    invoices = sorted(invoice_model.objects.filter(**{party_field: party}, docstatus=1, is_return=False,
                                                   outstanding_amount__gt=0).select_related("currency"),
                      key=lambda inv: (inv.due_date or inv.posting_date, inv.posting_date, inv.pk))
    return sources, invoices


def suggest(sources, invoices):
    """توزيع مقترح: أقدم مصدر على أقدم فاتورة بنفس العملة حتى ينفد أحدهما."""
    remaining = {inv.pk: flt(inv.outstanding_amount) for inv in invoices}
    lines = []
    for source in sources:
        left = source["available"]
        for inv in invoices:
            if left <= 0:
                break
            if inv.currency_id != source["currency"].pk or remaining[inv.pk] <= 0:
                continue
            share = min(left, remaining[inv.pk])
            lines.append({"source": source["key"], "invoice": inv.pk, "amount": share})
            remaining[inv.pk] -= share
            left -= share
    return lines


@login_required
def reconcile_tool(request):
    from buying.models import Supplier
    from core.permissions import require_submit
    from selling.models import Customer

    party_type = PartyType.SUPPLIER if request.GET.get("party_type") == PartyType.SUPPLIER else PartyType.CUSTOMER
    party_field = "customer" if party_type == PartyType.CUSTOMER else "supplier"
    party_model = Customer if party_type == PartyType.CUSTOMER else Supplier
    party = party_model.objects.filter(pk=request.GET.get("party") or 0).first()
    sources, invoices, lines = [], [], []
    if party is not None:
        sources, invoices = open_items(party_field, party)
        lines = suggest(sources, invoices)

    if request.method == "POST" and party is not None:
        by_key = {s["key"]: s for s in sources}
        by_invoice = {inv.pk: inv for inv in invoices}
        chosen = []
        for key, inv_id, amount in zip(request.POST.getlist("source"), request.POST.getlist("invoice"),
                                       request.POST.getlist("amount")):
            try:
                amount = flt(amount)
                inv_id = int(inv_id)
            except (ValueError, ArithmeticError):
                continue
            if amount > 0 and key in by_key and inv_id in by_invoice:
                chosen.append((by_key[key]["doc"], by_invoice[inv_id], amount))
        if not chosen:
            messages.error(request, "لم تُحدد أي مبالغ للتسوية.")
        else:
            try:
                with transaction.atomic():
                    doc = PaymentReconciliation.objects.create(
                        party_type=party_type, **{party_field: party}, created_by=request.user,
                        remarks=request.POST.get("remarks", "").strip())
                    for source, invoice, amount in chosen:
                        link = {"payment": source} if isinstance(source, PaymentEntry) else \
                            {f"{'sales' if party_field == 'customer' else 'purchase'}_return": source}
                        doc.lines.create(**link, amount=amount,
                                         **{f"{'sales' if party_field == 'customer' else 'purchase'}_invoice": invoice})
                    doc.calculate_totals()
                    doc.save()
                    try:
                        require_submit(request)
                    except ValidationError:
                        can_submit = False
                    else:
                        can_submit = True
                        doc.submit()
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                if can_submit:
                    messages.success(request, f"تمت التسوية بالمستند {doc}: {doc.total_allocated:,.2f}.")
                else:
                    messages.warning(request, f"حُفظت التسوية {doc} كمسودة؛ يعتمدها مستخدم لديه صلاحية الاعتماد.")
                return redirect(doc.get_absolute_url())

    return render(request, "accounts/reconcile.html", {
        "title": "تسوية الدفعات", "party_type": party_type, "party": party,
        "parties": party_model.objects.filter(disabled=False),
        "sources": sources, "invoices": invoices, "lines": lines,
        "total_sources": sum((s["available"] for s in sources), flt(0)),
        "total_invoices": sum((flt(i.outstanding_amount) for i in invoices), flt(0)),
    })
