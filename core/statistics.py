"""مركز التقارير والإحصائيات: كل تقارير الجهة في مكان واحد، ومؤشرات لحظية عن المستفيدين والموارد والبرامج."""
import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone

from core.utils import flt

REPORT_SECTIONS = ("تقارير", "التقارير", "القوائم")
REPORT_WORDS = ("تقرير", "قائمة ", "قائمة", "كشف", "أرصدة", "ميزان", "دفتر", "لوحة", "حزمة", "تحليل", "أعمار", "سجل الأصول")


def report_links(request):
    """تقارير ولوحات كل وحدة من القائمة نفسها (بحسب صلاحيات المستخدم)، مجمعة حسب الوحدة."""
    from core.navigation import build_menu

    groups = []
    for module in build_menu(request):
        if module["key"] in ("statistics", "platform"):
            continue
        links = []
        for section in module["sections"]:
            in_reports = any(word in (section.get("title") or "") for word in REPORT_SECTIONS)
            for item in section["items"]:
                if in_reports or any(word in item["label"] for word in REPORT_WORDS):
                    links.append(item)
        if links:
            groups.append({"label": module["label"], "group": module.get("group", ""), "links": links})
    return groups


def _year_bounds(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
    except ValueError:
        year = today.year
    return year, datetime.date(year, 1, 1), min(datetime.date(year, 12, 31), today) if year == today.year \
        else datetime.date(year, 12, 31)


def nonprofit_statistics(start, end):
    from beneficiaries.models import AidDisbursement, Beneficiary
    from funds.models import DonationReceipt

    beneficiaries = Beneficiary.objects.all()
    aid = AidDisbursement.objects.filter(docstatus=1, posting_date__gte=start, posting_date__lte=end)
    donations = DonationReceipt.objects.filter(docstatus=1, posting_date__gte=start, posting_date__lte=end)
    months = []
    for month in range(1, end.month + 1 if end.year == start.year else 13):
        first = datetime.date(start.year, month, 1)
        last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
        months.append({"month": first,
                       "donations": flt(donations.filter(posting_date__gte=first, posting_date__lte=last)
                                        .aggregate(t=Sum("amount"))["t"]),
                       "aid": flt(aid.filter(posting_date__gte=first, posting_date__lte=last)
                                  .aggregate(t=Sum("amount"))["t"])})
    stats = {
        "beneficiaries_total": beneficiaries.count(),
        "beneficiaries_approved": beneficiaries.filter(status="approved").count(),
        "aid_total": flt(aid.aggregate(t=Sum("amount"))["t"]), "aid_count": aid.count(),
        "aid_people": aid.values("beneficiary").distinct().count(),
        "donations_total": flt(donations.aggregate(t=Sum("amount"))["t"]), "donations_count": donations.count(),
        "donors": donations.exclude(donor=None).values("donor").distinct().count(),
        "by_category": beneficiaries.values("category__name").annotate(n=Count("id"),
                                                                       approved=Count("id", filter=Q(status="approved")))
        .order_by("-n"),
        "by_city": beneficiaries.exclude(city="").values("city").annotate(n=Count("id")).order_by("-n")[:10],
        "by_gender": beneficiaries.values("gender").annotate(n=Count("id")).order_by("-n"),
        "by_program": aid.values("program__name").annotate(n=Count("id"), people=Count("beneficiary", distinct=True),
                                                           total=Sum("amount")).order_by("-total"),
        "by_donation_type": donations.values("donation_type").annotate(n=Count("id"), total=Sum("amount"))
        .order_by("-total"),
        "top_donors": donations.values("donor_name").annotate(n=Count("id"), total=Sum("amount")).order_by("-total")[:10],
        "months": months,
    }
    labels = dict(DonationReceipt._meta.get_field("donation_type").choices)
    for row in stats["by_donation_type"]:
        row["label"] = labels.get(row["donation_type"], row["donation_type"])
    genders = dict(Beneficiary._meta.get_field("gender").choices)
    for row in stats["by_gender"]:
        row["label"] = genders.get(row["gender"], row["gender"])
    return stats


@login_required
def statistics_center(request):
    year, start, end = _year_bounds(request)
    company = request.company
    context = {"title": "مركز التقارير والإحصائيات", "groups": report_links(request), "year": year,
               "years": range(timezone.localdate().year, timezone.localdate().year - 5, -1),
               "from_date": start, "to_date": end}
    if company.is_nonprofit:
        context["stats"] = nonprofit_statistics(start, end)
    return render(request, "core/statistics.html", context)
