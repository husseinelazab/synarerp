from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render

from accounts.models import GLEntry, OpeningAccountBalance, OpeningPartyBalance
from core.models import DocStatus
from core.utils import flt
from stock.models import OpeningStock


@login_required
def opening_wizard(request):
    """معالج الأرصدة الافتتاحية: الخطوات الثلاث وحالة كل منها ورصيد حساب الأرصدة الافتتاحية."""
    steps = []
    for slug, model, title, text in (
        ("opening-account", OpeningAccountBalance, "أرصدة الحسابات",
         "البنوك والصناديق والأصول الثابتة والقروض ورأس المال وأي حساب آخر غير العملاء والموردين والمخزون."),
        ("opening-party", OpeningPartyBalance, "أرصدة العملاء والموردين",
         "المبالغ المستحقة على كل عميل ولكل مورد يوم بدء العمل على النظام، مع رقم الفاتورة القديمة."),
        ("opening-stock", OpeningStock, "المخزون الافتتاحي",
         "كميات وتكلفة كل صنف في كل مستودع، لتبدأ تكلفة البضاعة المباعة صحيحة من أول فاتورة."),
    ):
        steps.append({
            "slug": slug, "title": title, "text": text,
            "posted": model.objects.filter(docstatus=DocStatus.SUBMITTED).count(),
            "drafts": model.objects.filter(docstatus=DocStatus.DRAFT).count(),
        })
    opening = request.company.default_opening_account
    balance = None
    if opening:
        agg = GLEntry.objects.filter(account=opening, is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        balance = flt(agg["c"]) - flt(agg["d"])
    return render(request, "accounts/opening.html", {"steps": steps, "opening": opening, "balance": balance})
