"""التقارير المالية: ميزان المراجعة، دفتر الأستاذ، قائمة الدخل، الميزانية، الضريبة، الذمم."""
import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.models import Account, AccountType, GLEntry, RootType
from buying.models import PurchaseInvoice, Supplier
from core.models import DocStatus, FiscalYear
from core.utils import flt
from selling.models import Customer, SalesInvoice


def _period(request):
    today = timezone.localdate()
    fy = FiscalYear.objects.filter(start_date__lte=today, end_date__gte=today).first()
    default_from = fy.start_date if fy else datetime.date(today.year, 1, 1)
    from_date = parse_date(request.GET.get("from_date") or "") or default_from
    to_date = parse_date(request.GET.get("to_date") or "") or today
    return from_date, to_date


def _sums(qs, key="account_id"):
    return {
        row[key]: (flt(row["d"]), flt(row["c"]))
        for row in qs.values(key).annotate(d=Sum("debit"), c=Sum("credit"))
    }


def _active_gl():
    return GLEntry.objects.filter(is_cancelled=False)


def _level(request):
    try:
        return max(int(request.GET.get("level", 0)), 0) or None
    except ValueError:
        return None


@login_required
def trial_balance(request):
    from accounts.tree import max_tree_depth, tree_rows

    from_date, to_date = _period(request)
    level = _level(request)
    opening = _sums(_active_gl().filter(posting_date__lt=from_date))
    period = _sums(_active_gl().filter(posting_date__gte=from_date, posting_date__lte=to_date))
    raw = {}
    for account_id in set(opening) | set(period):
        od, oc = opening.get(account_id, (flt(0), flt(0)))
        pd, pc = period.get(account_id, (flt(0), flt(0)))
        raw[account_id] = [od, oc, pd, pc]

    def columns(values):
        od, oc, pd, pc = values
        ob, cb = od - oc, od - oc + pd - pc
        return [max(ob, 0), max(-ob, 0), pd, pc, max(cb, 0), max(-cb, 0)]

    rows = []
    for row in tree_rows(raw, 4, max_depth=level):
        row["columns"] = columns(row["values"])
        rows.append(row)
    # الإجمالي من الحسابات الفرعية فقط حتى لا تتكرر المبالغ
    totals = [flt(0)] * 6
    for values in raw.values():
        totals = [t + v for t, v in zip(totals, columns(values))]
    return render(request, "reports/trial_balance.html", {
        "title": "ميزان المراجعة", "from_date": from_date, "to_date": to_date, "rows": rows, "totals": totals,
        "level": level or 0, "levels": range(1, max_tree_depth() + 1),
    })


@login_required
def general_ledger(request):
    from_date, to_date = _period(request)
    accounts = Account.objects.filter(is_group=False)
    filters = Q()
    from accounts.models import CostCenter

    selected = {k: request.GET.get(k, "") for k in ("account", "customer", "supplier", "cost_center")}
    if selected["account"]:
        filters &= Q(account_id=selected["account"])
    if selected["customer"]:
        filters &= Q(customer_id=selected["customer"])
    if selected["supplier"]:
        filters &= Q(supplier_id=selected["supplier"])
    center, centers = _cost_center_filter(request)
    if centers is not None:
        filters &= Q(cost_center_id__in=centers)

    base = _active_gl().filter(filters)
    agg = base.filter(posting_date__lt=from_date).aggregate(d=Sum("debit"), c=Sum("credit"))
    balance = flt(agg["d"]) - flt(agg["c"])
    opening = balance
    rows = []
    entries = base.filter(posting_date__gte=from_date, posting_date__lte=to_date).select_related(
        "account", "customer", "supplier", "cost_center")
    for gle in entries:
        balance += gle.debit - gle.credit
        rows.append({"gle": gle, "balance": balance})
    total_debit = sum((r["gle"].debit for r in rows), flt(0))
    total_credit = sum((r["gle"].credit for r in rows), flt(0))
    return render(request, "reports/general_ledger.html", {
        "title": "دفتر الأستاذ العام", "from_date": from_date, "to_date": to_date,
        "accounts": accounts, "customers": Customer.objects.all(), "suppliers": Supplier.objects.all(),
        "cost_centers": CostCenter.objects.all(),
        "selected": selected, "rows": rows, "opening": opening, "closing": balance,
        "total_debit": total_debit, "total_credit": total_credit,
    })


CLOSING_VOUCHER = "period-closing"


def _cost_center_filter(request):
    """مركز التكلفة المختار مع كل المراكز الفرعية تحته."""
    from accounts.models import CostCenter

    center = CostCenter.objects.filter(pk=request.GET.get("cost_center") or 0).first()
    return center, (center.descendant_ids() if center else None)


def _grouped_balances(root_types, to_date, from_date=None, level=None, exclude_closing=False, cost_centers=None):
    """أقسام القوائم المالية بشكل شجري: لكل نوع جذري صفوفه (رؤوس بمجاميعها وفروع) وإجماليه."""
    from accounts.tree import tree_rows

    qs = _active_gl().filter(posting_date__lte=to_date, account__root_type__in=root_types)
    if from_date:
        qs = qs.filter(posting_date__gte=from_date)
    if exclude_closing:
        qs = qs.exclude(voucher_type=CLOSING_VOUCHER)
    if cost_centers is not None:
        qs = qs.filter(cost_center_id__in=cost_centers)
    sums = _sums(qs)
    sections = []
    for root in root_types:
        credit_nature = root in (RootType.INCOME, RootType.LIABILITY, RootType.EQUITY)
        values = {aid: [(c - d) if credit_nature else (d - c)] for aid, (d, c) in sums.items()}
        rows = tree_rows(values, 1, root_types=[root], max_depth=level)
        for row in rows:
            row["amount"] = row["values"][0]
        # الصف الجذري يمثل القسم نفسه؛ يُعرض إجماله في التذييل
        body = [row for row in rows if row["depth"] > 0]
        total = sum((row["amount"] for row in rows if row["depth"] == 0), flt(0))
        sections.append({"root": root, "label": RootType(root).label, "rows": body, "total": total})
    return sections


@login_required
def profit_and_loss(request):
    from accounts.tree import max_tree_depth

    from_date, to_date = _period(request)
    level = _level(request)
    from accounts.models import CostCenter

    center, centers = _cost_center_filter(request)
    income, expense = _grouped_balances([RootType.INCOME, RootType.EXPENSE], to_date, from_date, level=level,
                                        exclude_closing=True, cost_centers=centers)
    return render(request, "reports/statement.html", {
        "title": "قائمة الدخل" + (f" — {center}" if center else ""), "from_date": from_date, "to_date": to_date,
        "cost_centers": CostCenter.objects.all(), "selected_center": center.pk if center else None,
        "level": level or 0, "levels": range(2, max_tree_depth() + 1),
        "sections": [income, expense],
        "result_label": "صافي الربح / (الخسارة)", "result": income["total"] - expense["total"],
    })


@login_required
def balance_sheet(request):
    from accounts.tree import max_tree_depth

    from_date, to_date = _period(request)
    level = _level(request)
    assets, liabilities, equity = _grouped_balances([RootType.ASSET, RootType.LIABILITY, RootType.EQUITY], to_date,
                                                    level=level)
    income, expense = _grouped_balances([RootType.INCOME, RootType.EXPENSE], to_date)
    profit = income["total"] - expense["total"]
    if profit:
        equity["rows"].append({"account": "أرباح (خسائر) الفترة غير المرحلة", "amount": profit, "depth": 1,
                               "is_group": False})
        equity["total"] += profit
    return render(request, "reports/statement.html", {
        "title": "الميزانية العمومية (قائمة المركز المالي)", "to_date": to_date, "as_of": True,
        "level": level or 0, "levels": range(2, max_tree_depth() + 1),
        "sections": [assets, liabilities, equity],
        "result_label": "الخصوم + حقوق الملكية", "result": liabilities["total"] + equity["total"],
        "balanced": assets["total"] == liabilities["total"] + equity["total"],
    })


@login_required
def vat_report(request):
    """إقرار ضريبة القيمة المضافة مقسماً حسب بنود نموذج الإقرار لدى الهيئة."""
    from core.vat import VATCategory

    from_date, to_date = _period(request)
    flt_kw = {"docstatus": DocStatus.SUBMITTED, "posting_date__gte": from_date, "posting_date__lte": to_date}

    def grouped(model):
        rows = model.objects.filter(**flt_kw).values("vat_category", "vat_exemption_reason").annotate(
            net=Sum("base_net_total"), tax=Sum("base_tax_amount"))
        return [(r["vat_category"], r["vat_exemption_reason"], flt(r["net"]), flt(r["tax"])) for r in rows]

    def sales_line(category, reason):
        if category == VATCategory.STANDARD:
            return 1
        if category == VATCategory.ZERO:
            return 2 if reason in ("VATEX-SA-EDU", "VATEX-SA-HEA") else 4 if reason in ("VATEX-SA-32", "VATEX-SA-33") else 3
        return 5 if category == VATCategory.EXEMPT else None

    def purchase_line(category, reason):
        return {VATCategory.STANDARD: 7, VATCategory.ZERO: 10, VATCategory.EXEMPT: 11}.get(category)

    labels = {
        1: "المبيعات الخاضعة للنسبة الأساسية", 2: "المبيعات للمواطنين (خدمات صحية وتعليمية خاصة)",
        3: "المبيعات المحلية الخاضعة للنسبة الصفرية", 4: "الصادرات", 5: "المبيعات المعفاة",
        7: "المشتريات الخاضعة للنسبة الأساسية", 10: "المشتريات الخاضعة للنسبة الصفرية", 11: "المشتريات المعفاة",
    }
    lines = {n: {"no": n, "label": labels[n], "net": flt(0), "tax": flt(0)} for n in labels}
    out_of_scope = {"sales": flt(0), "purchases": flt(0)}
    for kind, model, mapper in (("sales", SalesInvoice, sales_line), ("purchases", PurchaseInvoice, purchase_line)):
        for category, reason, net, tax in grouped(model):
            line = mapper(category, reason)
            if line is None:
                out_of_scope[kind] += net
                continue
            lines[line]["net"] += net
            lines[line]["tax"] += tax
    sales_rows = [lines[n] for n in (1, 2, 3, 4, 5)]
    purchase_rows = [lines[n] for n in (7, 10, 11)]
    output_tax = sum((r["tax"] for r in sales_rows), flt(0))
    input_tax = sum((r["tax"] for r in purchase_rows), flt(0))
    return render(request, "reports/vat.html", {
        "title": "إقرار ضريبة القيمة المضافة", "from_date": from_date, "to_date": to_date,
        "sales_rows": sales_rows, "purchase_rows": purchase_rows, "out_of_scope": out_of_scope,
        "sales_net": sum((r["net"] for r in sales_rows), flt(0)), "output_tax": output_tax,
        "purchases_net": sum((r["net"] for r in purchase_rows), flt(0)), "input_tax": input_tax,
        "net_vat": output_tax - input_tax,
    })


@login_required
def party_balances(request, party):
    _, to_date = _period(request)
    if party == "customer":
        account_type, model, invoice_model, sign = AccountType.RECEIVABLE, Customer, SalesInvoice, 1
        title = "أرصدة العملاء (الذمم المدينة)"
    else:
        account_type, model, invoice_model, sign = AccountType.PAYABLE, Supplier, PurchaseInvoice, -1
        title = "أرصدة الموردين (الذمم الدائنة)"
    sums = _sums(
        _active_gl().filter(account__account_type=account_type, posting_date__lte=to_date), key=f"{party}_id"
    )
    rows = []
    for obj in model.objects.filter(id__in=[k for k in sums if k]):
        d, c = sums[obj.id]
        balance = (d - c) * sign
        if balance:
            rows.append({"party": obj, "debit": d, "credit": c, "balance": balance})
    invoices = invoice_model.objects.filter(docstatus=DocStatus.SUBMITTED).exclude(outstanding_amount=0)
    return render(request, "reports/party_balances.html", {
        "title": title, "to_date": to_date, "rows": rows, "party": party,
        "total": sum((r["balance"] for r in rows), flt(0)), "invoices": invoices, "today": timezone.localdate(),
    })


@login_required
def chart_of_accounts(request):
    sums = _sums(_active_gl())
    nodes = list(Account.objects.all())
    children = {}
    for acc in nodes:
        children.setdefault(acc.parent_id, []).append(acc)

    def walk(acc, depth):
        own_d, own_c = sums.get(acc.id, (flt(0), flt(0)))
        subtree = [walk(child, depth + 1) for child in children.get(acc.id, [])]
        balance = own_d - own_c + sum((s["balance"] for s in subtree), flt(0))
        return {"account": acc, "depth": depth, "balance": balance, "children": subtree}

    def flatten(node, out):
        out.append(node)
        for child in node["children"]:
            flatten(child, out)
        return out

    rows = []
    for root in children.get(None, []):
        flatten(walk(root, 0), rows)
    return render(request, "reports/chart_of_accounts.html", {"title": "شجرة الحسابات", "rows": rows})


@login_required
def general_journal(request):
    """دفتر اليومية العام: كل القيود (اليدوية والآلية من الفواتير والسندات والمخزون) مجمعة حسب المستند."""
    from django.core.paginator import Paginator

    from core.registry import REGISTRY

    from_date, to_date = _period(request)
    voucher_type = request.GET.get("voucher_type", "")
    entries = _active_gl().filter(posting_date__gte=from_date, posting_date__lte=to_date).select_related(
        "account", "customer", "supplier").order_by("posting_date", "voucher_type", "voucher_id", "id")
    if voucher_type:
        entries = entries.filter(voucher_type=voucher_type)

    vouchers, index = [], {}
    for gle in entries:
        key = (gle.voucher_type, gle.voucher_id)
        if key not in index:
            index[key] = {"type": gle.voucher_type, "id": gle.voucher_id, "no": gle.voucher_no,
                          "date": gle.posting_date, "lines": [], "debit": flt(0), "credit": flt(0)}
            vouchers.append(index[key])
        voucher = index[key]
        voucher["lines"].append(gle)
        voucher["debit"] += gle.debit
        voucher["credit"] += gle.credit

    page = Paginator(vouchers, 50).get_page(request.GET.get("page"))
    types = [(slug, dt.label) for slug, dt in REGISTRY.items() if dt.is_submittable]
    return render(request, "reports/general_journal.html", {
        "title": "دفتر اليومية العام", "from_date": from_date, "to_date": to_date, "page": page,
        "types": types, "voucher_type": voucher_type,
        "total_debit": sum((v["debit"] for v in vouchers), flt(0)),
        "total_credit": sum((v["credit"] for v in vouchers), flt(0)),
        "count": len(vouchers),
    })


@login_required
def aging_report(request, party):
    """أعمار الديون: العملاء (المدينون) أو الموردون (الدائنون) بفئات التأخير."""
    from accounts.credit import BUCKETS, aging

    if party not in ("customer", "supplier"):
        party = "customer"
    basis = "posting" if request.GET.get("basis") == "posting" else "due"
    rows = aging(party, basis)
    only = request.GET.get("only", "")
    if only == "overdue":
        rows = [r for r in rows if r["overdue"] > 0]
    elif only == "over_limit":
        rows = [r for r in rows if r["over_limit"]]
    rows.sort(key=lambda r: (-r["overdue"], -r["outstanding"]))
    totals = [sum((r["buckets"][i] for r in rows), flt(0)) for i in range(len(BUCKETS))]
    from messaging.models import MessagingSettings

    cfg = MessagingSettings.all_objects.filter(company=request.company).first()
    return render(request, "reports/aging.html", {
        "title": "أعمار ديون العملاء" if party == "customer" else "أعمار ديون الموردين", "party": party,
        "rows": rows, "buckets": [b[1] for b in BUCKETS], "bucket_totals": list(zip([b[1] for b in BUCKETS], totals)),
        "basis": basis, "only": only,
        "grand": sum(totals, flt(0)), "messaging_active": bool(cfg and cfg.active),
    })


@login_required
def cheques_due(request):
    """الشيكات المفتوحة حسب الاستحقاق: المتأخرة، هذا الأسبوع، هذا الشهر، لاحقاً — للوارد والصادر."""
    from accounts.models import Cheque, ChequeDirection, ChequeStatus

    today = timezone.localdate()
    open_statuses = [ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED, ChequeStatus.ISSUED]
    groups = [("متأخرة", None, today - datetime.timedelta(days=1)),
              ("خلال 7 أيام", today, today + datetime.timedelta(days=7)),
              ("خلال 30 يوماً", today + datetime.timedelta(days=8), today + datetime.timedelta(days=30)),
              ("لاحقاً", today + datetime.timedelta(days=31), None)]
    result = []
    for direction, label in ChequeDirection.choices:
        cheques = Cheque.objects.filter(docstatus=1, direction=direction, status__in=open_statuses).select_related(
            "customer", "supplier")
        sections = []
        for title, start, end in groups:
            qs = cheques
            if start:
                qs = qs.filter(cheque_date__gte=start)
            if end:
                qs = qs.filter(cheque_date__lte=end)
            rows = list(qs.order_by("cheque_date"))
            sections.append({"title": title, "rows": rows, "total": sum((c.amount for c in rows), flt(0))})
        result.append({"direction": direction, "label": label, "sections": sections,
                       "total": sum((s["total"] for s in sections), flt(0))})
    bounced = Cheque.objects.filter(status=ChequeStatus.BOUNCED, bounced_on__gte=today - datetime.timedelta(days=90))
    return render(request, "reports/cheques_due.html", {"title": "الشيكات المستحقة", "groups": result,
                                                        "bounced": bounced.select_related("customer", "supplier"),
                                                        "today": today})


@login_required
def withholding_report(request):
    """ضريبة الاستقطاع المستحقة على المدفوعات لغير المقيمين حسب الشهر (لإقرار الاستقطاع قبل اليوم العاشر من الشهر التالي)."""
    from accounts.models import PaymentEntry

    from_date, to_date = _period(request)
    payments = (PaymentEntry.objects.filter(docstatus=1, withholding_amount__gt=0, posting_date__gte=from_date,
                                            posting_date__lte=to_date).select_related("supplier", "currency")
                .order_by("posting_date"))
    groups = {}
    for p in payments:
        key = (p.supplier.withholding_type or "غير محدد", p.withholding_rate)
        g = groups.setdefault(key, {"type": key[0], "rate": key[1], "gross": flt(0), "tax": flt(0), "count": 0})
        g["gross"] += p.to_base(p.amount)
        g["tax"] += p.to_base(p.withholding_amount)
        g["count"] += 1
    return render(request, "reports/withholding.html", {
        "title": "إقرار ضريبة الاستقطاع", "from_date": from_date, "to_date": to_date, "payments": payments,
        "groups": list(groups.values()), "total_tax": sum((g["tax"] for g in groups.values()), flt(0)),
    })
