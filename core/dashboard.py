"""بيانات لوحة التحكم: مؤشرات الشهر، ما يحتاج انتباهاً، ومخطط المبيعات والمشتريات."""
import datetime
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.models import DocStatus
from core.utils import flt

MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


def _month_start(date, back=0):
    month = date.month - 1 - back
    return datetime.date(date.year + month // 12, month % 12 + 1, 1)


def _gl_balance(**filters):
    from accounts.models import GLEntry

    agg = GLEntry.objects.filter(is_cancelled=False, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return flt(agg["d"]) - flt(agg["c"])


def _net(model, start, end):
    return flt(model.objects.filter(docstatus=DocStatus.SUBMITTED, posting_date__gte=start,
                                    posting_date__lt=end).aggregate(v=Sum("base_net_total"))["v"])


def _change(current, previous):
    if not previous:
        return None
    return round(float((current - previous) / abs(previous) * 100))


def _nice_max(value):
    if value <= 0:
        return Decimal(1000)
    exponent = len(str(int(value))) - 1
    base = Decimal(10) ** exponent
    for step in (1, 2, 2.5, 5, 10):
        if value <= base * Decimal(str(step)):
            return base * Decimal(str(step))
    return base * 10


def _bar_path(x, y, width, height, radius=4):
    radius = min(radius, height)
    if height <= 0:
        return ""
    return (f"M{x:.1f},{y + height:.1f} V{y + radius:.1f} Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f} "
            f"H{x + width - radius:.1f} Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + radius:.1f} V{y + height:.1f} Z")


def sales_purchases_chart(today, months=6):
    from buying.models import PurchaseInvoice
    from selling.models import SalesInvoice

    data = []
    for back in range(months - 1, -1, -1):
        start, end = _month_start(today, back), _month_start(today, back - 1)
        data.append({"label": MONTHS[start.month - 1], "sales": _net(SalesInvoice, start, end),
                     "purchases": _net(PurchaseInvoice, start, end)})

    width, height, top, bottom, axis = 640, 230, 16, 28, 56
    plot_w, plot_h = width - axis - 8, height - top - bottom
    peak = _nice_max(max([max(d["sales"], d["purchases"]) for d in data] + [Decimal(0)]))
    group, bar, gap = plot_w / months, 22, 2
    for index, entry in enumerate(data):
        # الاتجاه من اليمين لليسار: أقدم شهر يمين محور القيم
        center = width - axis - group * index - group / 2
        entry["x_label"] = center
        bars = []
        # المبيعات يمين مركز الشهر والمشتريات يساره، بفاصل 2px بين العمودين
        for key, x in (("sales", center + gap / 2), ("purchases", center - gap / 2 - bar)):
            value = entry[key]
            h = float(value / peak) * plot_h if value > 0 else 0
            bars.append({"key": key, "value": value, "d": _bar_path(x, top + plot_h - h, bar, h)})
        entry["bars"] = bars
    ticks = [{"y": top + plot_h - plot_h * i / 4, "value": peak * i / 4} for i in range(5)]
    return {"data": data, "ticks": ticks, "width": width, "height": height, "baseline": top + plot_h,
            "axis_x": width - axis + 6, "plot_left": 8, "plot_right": width - axis}


def dashboard_context(request):
    from accounts.models import AccountType, RootType
    from buying.models import PurchaseInvoice
    from core.registry import REGISTRY
    from selling.models import SalesInvoice
    from stock.models import Bin

    today = timezone.localdate()
    this_month, last_month, next_month = _month_start(today), _month_start(today, 1), _month_start(today, -1)

    sales, sales_prev = _net(SalesInvoice, this_month, next_month), _net(SalesInvoice, last_month, this_month)
    purchases, purchases_prev = _net(PurchaseInvoice, this_month, next_month), _net(PurchaseInvoice, last_month, this_month)
    from accounts.models import GLEntry

    month = GLEntry.objects.filter(is_cancelled=False, posting_date__gte=this_month,
                                   posting_date__lt=next_month).exclude(voucher_type="period-closing")
    totals = {row["account__root_type"]: flt(row["d"]) - flt(row["c"]) for row in month.filter(
        account__root_type__in=[RootType.INCOME, RootType.EXPENSE]).values("account__root_type").annotate(
        d=Sum("debit"), c=Sum("credit"))}
    profit = -totals.get(RootType.INCOME, 0) - totals.get(RootType.EXPENSE, 0)
    cash = _gl_balance(account__account_type__in=[AccountType.CASH, AccountType.BANK])

    kpis = [
        {"label": "صافي المبيعات هذا الشهر", "value": sales, "change": _change(sales, sales_prev),
         "hint": "قبل الضريبة وبعد المرتجعات", "url": reverse("doc_list", args=["sales-invoice"])},
        {"label": "صافي المشتريات هذا الشهر", "value": purchases, "change": _change(purchases, purchases_prev),
         "hint": "قبل الضريبة وبعد المرتجعات", "url": reverse("doc_list", args=["purchase-invoice"])},
        {"label": "صافي الربح هذا الشهر", "value": profit, "hint": "الإيرادات − المصروفات",
         "url": reverse("profit_and_loss") + f"?from_date={this_month:%Y-%m-%d}"},
        {"label": "النقدية والبنوك", "value": cash, "hint": "الرصيد الحالي", "url": reverse("chart_of_accounts")},
    ]
    company = request.company
    balances = [
        ("مستحقات لك من العملاء", _gl_balance(account__account_type=AccountType.RECEIVABLE),
         reverse("party_balances", args=["customer"])),
        ("مستحقات عليك للموردين", -_gl_balance(account__account_type=AccountType.PAYABLE),
         reverse("party_balances", args=["supplier"])),
        ("قيمة المخزون", flt(Bin.objects.aggregate(v=Sum("stock_value"))["v"]), reverse("stock_balance")),
    ]
    vat = -_gl_balance(account=company.default_output_tax_account) - _gl_balance(account=company.default_input_tax_account)
    balances.append(("ضريبة القيمة المضافة المستحقة" if vat >= 0 else "ضريبة القيمة المضافة القابلة للاسترداد",
                     abs(vat), reverse("vat_report")))

    drafts = []
    for slug, dt in REGISTRY.items():
        if dt.is_submittable:
            count = dt.model.objects.filter(docstatus=DocStatus.DRAFT).count()
            if count:
                drafts.append({"label": dt.label_plural, "count": count,
                               "url": reverse("doc_list", args=[slug]) + "?status=0"})
    overdue = SalesInvoice.objects.filter(docstatus=DocStatus.SUBMITTED, outstanding_amount__gt=0, due_date__lt=today)
    overdue_info = overdue.aggregate(v=Sum("outstanding_amount"))

    recent = sorted(
        list(SalesInvoice.objects.select_related("customer")[:6]) + list(PurchaseInvoice.objects.select_related("supplier")[:6]),
        key=lambda doc: doc.created_at, reverse=True,
    )[:8]
    return {
        "today": today, "kpis": kpis, "balances": balances, "drafts": drafts,
        "overdue_count": overdue.count(), "overdue_amount": flt(overdue_info["v"]),
        "chart": sales_purchases_chart(today), "recent": recent,
    }
