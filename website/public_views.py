"""صفحات موقع الجمعية العام."""
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from core.utils import flt
from website.site import site_view


def impact_stats():
    """أرقام الأثر الحية من وحدات النظام للواجهة."""
    from beneficiaries.models import AidDisbursement, Beneficiary
    from volunteering.models import VolunteerParticipation

    year = timezone.localdate().year
    return [
        ("مستفيد معتمد", Beneficiary.objects.filter(status="approved").count()),
        ("مساعدة صُرفت هذا العام", AidDisbursement.objects.filter(docstatus=1, posting_date__year=year).count()),
        ("ساعة تطوع هذا العام", int(flt(VolunteerParticipation.objects.filter(
            status="completed", opportunity__start_date__year=year).aggregate(t=Sum("hours"))["t"]))),
    ]


@site_view
def home(request):
    return render(request, "website/home.html", {"stats": impact_stats()})


def _placeholder(title):
    @site_view
    def view(request, *args, **kwargs):
        return render(request, "website/placeholder.html", {"page_title": title})
    return view


about = _placeholder("عن الجمعية")
governance = _placeholder("الحوكمة والإفصاح")
programs = _placeholder("البرامج والمشاريع")
news = _placeholder("المركز الإعلامي")
news_detail = _placeholder("خبر")
gallery = _placeholder("معرض الصور")
apply = _placeholder("طلب مساعدة")
contact = _placeholder("تواصل معنا")
