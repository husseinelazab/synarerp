import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render

from assets.models import Asset, AssetCategory, AssetDepreciation, AssetStatus
from assets.services import dispose, pending_rows, post_depreciation
from core.forms import formfield_callback, style_form
from core.models import DocStatus
from core.permissions import require_submit
from core.utils import flt


def _error(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required
def depreciation(request):
    """ترحيل أقساط الإهلاك المستحقة حتى تاريخ (عادة نهاية الشهر)."""
    today = datetime.date.today()
    try:
        upto = datetime.date.fromisoformat(request.POST.get("upto") or request.GET.get("upto") or "")
    except ValueError:
        upto = today.replace(day=1) - datetime.timedelta(days=1)  # نهاية الشهر الماضي
    if request.method == "POST":
        try:
            require_submit(request)
            entries = post_depreciation(upto, user=request.user)
        except ValidationError as exc:
            messages.error(request, _error(exc))
        else:
            if entries:
                messages.success(request, f"رُحّل الإهلاك في {len(entries)} قيد: " + "، ".join(e.name for e in entries))
            else:
                messages.info(request, "لا توجد أقساط مستحقة حتى هذا التاريخ.")
        return redirect(f"{request.path}?upto={upto:%Y-%m-%d}")
    rows = list(pending_rows(upto))
    return render(request, "assets/depreciation.html", {
        "upto": upto, "rows": rows, "total": flt(sum(r.amount for r in rows)),
    })


class DisposeForm(forms.Form):
    date = forms.DateField(label="تاريخ البيع / الاستبعاد", widget=forms.DateInput(attrs={"type": "date"}))
    sale_amount = forms.DecimalField(label="ثمن البيع (بدون الضريبة)", required=False, min_value=0, decimal_places=2,
                                     help_text="اتركه صفراً للاستبعاد أو الإتلاف دون بيع")
    receive_account = forms.ModelChoiceField(label="استُلم في حساب", required=False, queryset=None)
    customer = forms.ModelChoiceField(label="العميل (إن كان البيع آجلاً)", required=False, queryset=None)
    vat = forms.BooleanField(label="البيع خاضع لضريبة القيمة المضافة", required=False,
                             help_text="تُضاف الضريبة على الثمن وتُرحّل لحساب ضريبة المخرجات")

    def __init__(self, *args, **kwargs):
        from accounts.models import Account, AccountType
        from selling.models import Customer

        super().__init__(*args, **kwargs)
        self.fields["receive_account"].queryset = Account.objects.filter(
            is_group=False, disabled=False,
            account_type__in=[AccountType.CASH, AccountType.BANK, AccountType.RECEIVABLE])
        self.fields["customer"].queryset = Customer.objects.all()
        style_form(self)


@login_required
def dispose_asset(request, pk):
    asset = get_object_or_404(Asset, pk=pk, docstatus=DocStatus.SUBMITTED)
    if asset.status == AssetStatus.DISPOSED:
        messages.info(request, "الأصل مستبعد مسبقاً.")
        return redirect(asset.get_absolute_url())
    form = DisposeForm(request.POST or None, initial={"date": datetime.date.today()})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            require_submit(request)
            je = dispose(asset, data["date"], data["sale_amount"], data["receive_account"], data["customer"],
                         data["vat"], user=request.user)
        except ValidationError as exc:
            form.add_error(None, _error(exc))
        else:
            messages.success(request, f"تم {'بيع' if data['sale_amount'] else 'استبعاد'} الأصل وترحيل القيد {je}.")
            return redirect(asset.get_absolute_url())
    return render(request, "assets/dispose.html", {"form": form, "asset": asset})


@login_required
def asset_register(request):
    """سجل الأصول: التكلفة ومجمع الإهلاك والقيمة الدفترية كما في تاريخ."""
    try:
        as_of = datetime.date.fromisoformat(request.GET.get("as_of", ""))
    except ValueError:
        as_of = datetime.date.today()
    assets = Asset.objects.filter(docstatus=DocStatus.SUBMITTED, purchase_date__lte=as_of).select_related(
        "category", "custodian").order_by("category__name", "name")
    rows, groups = [], {}
    for a in assets:
        if a.disposal_date and a.disposal_date <= as_of:
            continue
        posted = AssetDepreciation.objects.filter(asset=a, journal_entry__isnull=False,
                                                  schedule_date__lte=as_of).aggregate(t=Sum("amount"))["t"] or 0
        accumulated = flt(a.opening_accumulated_depreciation + posted)
        row = {"asset": a, "cost": a.gross_amount, "accumulated": accumulated, "book": flt(a.gross_amount - accumulated)}
        rows.append(row)
        g = groups.setdefault(a.category.name, {"cost": 0, "accumulated": 0, "book": 0, "count": 0})
        for key in ("cost", "accumulated", "book"):
            g[key] += row[key]
        g["count"] += 1
    totals = {key: sum(g[key] for g in groups.values()) for key in ("cost", "accumulated", "book")}
    return render(request, "assets/register.html", {"rows": rows, "groups": groups, "totals": totals, "as_of": as_of})
