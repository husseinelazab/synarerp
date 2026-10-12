"""المعاملات بين الشركات الشقيقة على المنصة، والقوائم المالية الموحدة."""
from django.core.exceptions import ValidationError
from django.db import transaction

from core.models import Membership, Role
from core.notify import notify_managers
from core.tenancy import tenant_context


def managed_companies(user):
    """الشركات التي يديرها المستخدم (مالك أو مدير)."""
    from core.models import Company

    ids = Membership.objects.filter(user=user, is_active=True, role__in=[Role.OWNER, Role.ADMIN]).values("company_id")
    return Company.objects.filter(id__in=ids).order_by("name")


def _can_link(user, source, target):
    return user is not None and target in managed_companies(user) and source in managed_companies(user)


def mirror_sales_invoice(invoice):
    """فاتورة مبيعات لشركة شقيقة ← فاتورة مشتريات مسودة في الشركة الشقيقة بنفس الأصناف (بالكود) والأسعار.

    لا تُنشأ إلا إن كان منشئ الفاتورة يدير الشركتين؛ وما تعذر يُبلّغ به مديرو الشركة الشقيقة.
    """
    from buying.models import PurchaseInvoice, Supplier
    from stock.models import Item, Warehouse

    source, target = invoice.company, invoice.customer.represents_company
    if target is None or target.pk == source.pk:
        return None
    if not _can_link(invoice.created_by, source, target):
        notify_managers(source, f"لم تُنشأ فاتورة مشتريات في «{target}» للفاتورة {invoice}: منشئها لا يدير الشركتين.",
                        invoice.get_absolute_url())
        return None
    rows = list(invoice.items.select_related("item"))
    with tenant_context(target):
        try:
            with transaction.atomic():
                supplier = Supplier.objects.filter(represents_company=source).first()
                if supplier is None:
                    supplier, _ = Supplier.objects.get_or_create(name=source.name, defaults={
                        "represents_company": source, "tax_id": source.tax_id})
                    if supplier.represents_company_id != source.pk:
                        supplier.represents_company = source
                        supplier.save(update_fields=["represents_company"])
                codes = {r.item.code for r in rows}
                items = {i.code: i for i in Item.objects.filter(code__in=codes)}
                missing = sorted(codes - set(items))
                if missing:
                    raise ValidationError(f"أصناف غير موجودة بنفس الكود: {'، '.join(missing)}")
                bill = PurchaseInvoice.objects.create(
                    company=target, supplier=supplier, bill_no=invoice.name, intercompany_reference=invoice.name,
                    posting_date=invoice.posting_date, currency=invoice.currency, exchange_rate=invoice.exchange_rate,
                    tax_rate=invoice.tax_rate, vat_category=invoice.vat_category,
                    vat_exemption_reason=invoice.vat_exemption_reason, update_stock=invoice.update_stock,
                    warehouse=Warehouse.objects.filter(disabled=False).first(), due_date=invoice.due_date,
                    remarks=f"مشتريات من الشركة الشقيقة {source} — فاتورتها {invoice.name}")
                for r in rows:
                    rate = r.rate
                    if invoice.prices_include_tax and invoice.tax_rate:
                        rate = rate * 100 / (100 + invoice.tax_rate)
                    bill.items.create(item=items[r.item.code], qty=r.stock_qty, rate=round(rate / (r.conversion_factor or 1), 2),
                                      serial_nos=r.serial_nos)
                bill.calculate_totals()
                bill.save()
        except ValidationError as exc:
            notify_managers(target, f"تعذر إنشاء فاتورة مشتريات من فاتورة {source} رقم {invoice.name}: "
                                    f"{' '.join(exc.messages)}", "/app/purchase-invoice/")
            return None
        notify_managers(target, f"فاتورة مشتريات مسودة {bill} من الشركة الشقيقة {source} — راجعها واعتمدها",
                        bill.get_absolute_url())
    return bill


def consolidated_statement(companies, kind, from_date, to_date):
    """قائمة موحدة لعدة شركات بنفس دليل الحسابات (تُطابق الحسابات برقمها):
    عمود لكل شركة، وعمود الاستبعادات (أرصدة ومبيعات الشركات الشقيقة فيما بينها)، والموحد."""
    from django.db.models import Sum

    from accounts.models import Account, GLEntry, RootType
    from buying.models import PurchaseInvoice
    from core.utils import flt
    from selling.models import SalesInvoice

    roots = [RootType.INCOME, RootType.EXPENSE] if kind == "pl" else [RootType.ASSET, RootType.LIABILITY,
                                                                       RootType.EQUITY]
    ids = [c.pk for c in companies]
    group_ids = set(ids)
    accounts = {}
    values = {}
    for company in companies:
        qs = GLEntry.all_objects.filter(company=company, is_cancelled=False, account__root_type__in=roots,
                                        posting_date__lte=to_date)
        if kind == "pl":
            qs = qs.filter(posting_date__gte=from_date).exclude(voucher_type="period-closing")
        for row in qs.values("account__number", "account__name", "account__root_type").annotate(
                d=Sum("debit"), c=Sum("credit")):
            key = row["account__number"] or row["account__name"]
            accounts.setdefault(key, (row["account__name"], row["account__root_type"]))
            credit_nature = row["account__root_type"] in (RootType.INCOME, RootType.LIABILITY, RootType.EQUITY)
            amount = flt(row["c"]) - flt(row["d"]) if credit_nature else flt(row["d"]) - flt(row["c"])
            values.setdefault(key, {})[company.pk] = amount
    eliminations = {}

    def eliminate(key, amount):
        eliminations[key] = eliminations.get(key, flt(0)) + amount

    for company in companies:
        if kind == "bs":
            # ذمم الشركات الشقيقة فيما بينها
            for field, root in (("customer", RootType.ASSET), ("supplier", RootType.LIABILITY)):
                rows = (GLEntry.all_objects.filter(company=company, is_cancelled=False, posting_date__lte=to_date,
                                                   **{f"{field}__represents_company_id__in": group_ids - {company.pk}})
                        .values("account__number", "account__name").annotate(d=Sum("debit"), c=Sum("credit")))
                for row in rows:
                    key = row["account__number"] or row["account__name"]
                    amount = flt(row["d"]) - flt(row["c"])
                    eliminate(key, -(amount if root == RootType.ASSET else -amount))
        else:
            # المبيعات بين الشركات تُستبعد من الإيرادات، ومقابلها من مشتريات الشركة الشقيقة (مصروفات/تكلفة)
            sales = SalesInvoice.all_objects.filter(company=company, docstatus=1, posting_date__gte=from_date,
                                                    posting_date__lte=to_date,
                                                    customer__represents_company_id__in=group_ids - {company.pk})
            for row in (GLEntry.all_objects.filter(company=company, is_cancelled=False, voucher_type="sales-invoice",
                                                   voucher_id__in=sales.values("id"), account__root_type=RootType.INCOME)
                        .values("account__number", "account__name").annotate(d=Sum("debit"), c=Sum("credit"))):
                eliminate(row["account__number"] or row["account__name"], -(flt(row["c"]) - flt(row["d"])))
            buys = PurchaseInvoice.all_objects.filter(company=company, docstatus=1, posting_date__gte=from_date,
                                                      posting_date__lte=to_date,
                                                      supplier__represents_company_id__in=group_ids - {company.pk})
            for row in (GLEntry.all_objects.filter(company=company, is_cancelled=False, voucher_type="purchase-invoice",
                                                   voucher_id__in=buys.values("id"), account__root_type=RootType.EXPENSE)
                        .values("account__number", "account__name").annotate(d=Sum("debit"), c=Sum("credit"))):
                eliminate(row["account__number"] or row["account__name"], -(flt(row["d"]) - flt(row["c"])))
    rows = []
    for key in sorted(accounts, key=lambda k: (roots.index(accounts[k][1]), str(k))):
        name, root = accounts[key]
        per = [values.get(key, {}).get(c, flt(0)) for c in ids]
        elim = eliminations.get(key, flt(0))
        total = sum(per, flt(0)) + elim
        if any(per) or elim:
            rows.append({"key": key, "name": name, "root": root, "values": per, "elimination": elim, "total": total})
    sections = []
    for root in roots:
        section_rows = [r for r in rows if r["root"] == root]
        sections.append({"label": RootType(root).label, "rows": section_rows,
                         "totals": [sum((r["values"][i] for r in section_rows), flt(0)) for i in range(len(ids))],
                         "elimination": sum((r["elimination"] for r in section_rows), flt(0)),
                         "total": sum((r["total"] for r in section_rows), flt(0))})
    if kind == "bs":
        import datetime

        income, expense = consolidated_statement(companies, "pl", datetime.date(1900, 1, 1), to_date)
        profit = {"key": "", "name": "أرباح (خسائر) غير مرحلة", "root": RootType.EQUITY,
                  "values": [i - e for i, e in zip(income["totals"], expense["totals"])],
                  "elimination": income["elimination"] - expense["elimination"],
                  "total": income["total"] - expense["total"]}
        equity = sections[2]
        equity["rows"].append(profit)
        equity["totals"] = [t + v for t, v in zip(equity["totals"], profit["values"])]
        equity["elimination"] += profit["elimination"]
        equity["total"] += profit["total"]
    return sections
