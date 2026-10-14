"""صفحات موقع الجمعية العام."""
import logging
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from core.utils import flt
from programs import views as program_views
from programs.models import TrainingProgram
from relations import views as relations_views
from relations.models import Survey
from volunteering import views as volunteer_views
from volunteering.models import VolunteerOpportunity
from website.forms import ApplyForm, ContactForm
from website.models import (APPLY_NOTE, WEB_SUBMISSION, Album, DocumentCategory, NewsCategory, NewsPost, Partner,
                            PublishedDocument)
from website.site import site_url, site_view

logger = logging.getLogger(__name__)
NEWS_PER_PAGE = 9


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


def erp_url(name, *args):
    """رابط صفحة في النظام (للتنبيهات) حتى لو كان الطلب على النطاق المستقل بمسارات الموقع."""
    return reverse(name, args=args, urlconf=settings.ROOT_URLCONF)


def _notify(company, title, url, body=""):
    from core.notify import notify_managers

    try:
        notify_managers(company, title, url, body)
    except Exception:  # تعذر البريد لا يُفشل استقبال الطلب
        logger.exception("website notification failed")


# ---------------------------------------------------------------- مصادر المحتوى من وحدات النظام

def open_training(request, limit=None):
    from programs.models import ProgramStatus, TrainingProgram

    qs = TrainingProgram.objects.filter(status=ProgramStatus.OPEN, end_date__gte=timezone.localdate()).order_by(
        "start_date")
    items = list(qs[:limit] if limit else qs)
    for p in items:
        p.public_link = site_url(request, "program_register", p.public_token)
    return items


def running_training():
    from programs.models import ProgramStatus, TrainingProgram

    return list(TrainingProgram.objects.filter(status=ProgramStatus.RUNNING).order_by("start_date")[:12])


def open_opportunities(request, limit=None):
    from volunteering.models import OpportunityStatus, VolunteerOpportunity

    qs = VolunteerOpportunity.objects.filter(status=OpportunityStatus.OPEN, end_date__gte=timezone.localdate()).order_by(
        "start_date")
    items = list(qs[:limit] if limit else qs)
    for o in items:
        o.public_link = site_url(request, "volunteer_register", o.public_token)
    return items


def open_surveys(request, limit=None):
    from django.db.models import Q

    from relations.models import Survey

    qs = Survey.objects.filter(is_open=True).filter(Q(end_date=None) | Q(end_date__gte=timezone.localdate()))
    items = list(qs[:limit] if limit else qs)
    for s in items:
        s.public_link = site_url(request, "survey_respond", s.public_token)
    return items


def featured_opportunities(request, limit=6):
    """فرص التبرع المميزة من المتجر بروابط داخل الموقع."""
    try:
        from store.public import featured_opportunities as store_featured

        with transaction.atomic():  # خطأ في المتجر لا يُفسد معاملة الطلب
            items = list(store_featured(limit) or [])
    except Exception:
        logger.exception("store featured opportunities failed")
        return []
    result = []
    for item in items:
        try:
            item = dict(item, link=site_url(request, item["url_name"], *item.get("url_args", ())))
        except Exception:
            continue
        result.append(item)
    return result


def current_plan():
    from excellence.models import PlanStatus, StrategicPlan

    plan = StrategicPlan.current()
    return plan if plan is not None and plan.status != PlanStatus.DRAFT else None


def current_board():
    from governance.models import BoardTerm

    term = BoardTerm.current()
    if term is None:
        return None, []
    seats = list(term.seats.select_related("member"))
    order = ["chair", "vice", "treasurer", "secretary", "member"]
    seats.sort(key=lambda s: (order.index(s.position) if s.position in order else 99, s.pk))
    return term, seats


# ---------------------------------------------------------------- الصفحات

@site_view
def home(request):
    return render(request, "website/home.html", {
        "stats": impact_stats(),
        "featured": featured_opportunities(request),
        "latest_news": list(NewsPost.objects.filter(is_published=True)[:3]),
        "training": open_training(request, 6),
        "opportunities": open_opportunities(request, 6),
        "surveys": open_surveys(request, 4),
        "partners": list(Partner.objects.filter(is_published=True)),
    })


@site_view
def about(request):
    term, seats = current_board()
    plan = current_plan()
    return render(request, "website/about.html", {
        "plan": plan, "goals": list(plan.goals.all()) if plan else [], "term": term, "seats": seats,
    })


@site_view
def governance(request):
    docs = PublishedDocument.objects.filter(is_published=True).exclude(size=0).defer("content")
    groups = []
    for value, label in DocumentCategory.choices:
        items = [d for d in docs if d.category == value]
        if items:
            groups.append((label, items))
    term, seats = current_board()
    return render(request, "website/governance.html", {"groups": groups, "term": term, "seats": seats})


@site_view
def document(request, pk):
    doc = get_object_or_404(PublishedDocument, pk=pk, is_published=True, company=request.site.company)
    if not doc.size:
        raise Http404
    response = HttpResponse(bytes(doc.content), content_type=doc.content_type or "application/octet-stream")
    response["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(doc.file_name or 'document')}"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@site_view
def programs(request):
    from beneficiaries.models import AidProgram
    from projects.models import Project, ProjectStatus

    projects = list(Project.objects.filter(status__in=[ProjectStatus.ACTIVE, ProjectStatus.PLANNED,
                                                       ProjectStatus.COMPLETED]).only(
        "name", "description", "status", "start_date", "end_date", "progress_source", "manual_progress")[:24])
    return render(request, "website/programs.html", {
        "aid_programs": list(AidProgram.objects.filter(is_active=True).only("name", "description", "aid_type")),
        "training": open_training(request), "running": running_training(),
        "opportunities": open_opportunities(request), "projects": projects,
    })


@site_view
def news(request):
    qs = NewsPost.objects.filter(is_published=True)
    category = request.GET.get("category", "")
    if category in NewsCategory.values:
        qs = qs.filter(category=category)
    else:
        category = ""
    page = Paginator(qs, NEWS_PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "website/news.html", {"page": page, "category": category,
                                                 "categories": NewsCategory.choices})


@site_view
def news_detail(request, pk):
    post = get_object_or_404(NewsPost, pk=pk, is_published=True, company=request.site.company)
    related = NewsPost.objects.filter(is_published=True).exclude(pk=post.pk)[:3]
    return render(request, "website/news_detail.html", {
        "post": post, "related": related,
        "canonical": request.build_absolute_uri(site_url(request, "site_news_detail", post.pk)),
    })


@site_view
def gallery(request):
    albums = Album.objects.filter(is_published=True, photos__isnull=False).distinct()
    return render(request, "website/gallery.html", {"albums": albums})


@site_view
def album(request, pk):
    obj = get_object_or_404(Album, pk=pk, is_published=True, company=request.site.company)
    return render(request, "website/album.html", {"album": obj, "photos": obj.photos.all()})


@site_view
def apply(request):
    from beneficiaries.models import Beneficiary, FamilyMember

    form = ApplyForm(request.POST or None)
    reference = None
    done = False
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        company = request.site.company
        if data["website"]:
            done = True  # بوت: لا نحفظ شيئاً
        elif (Beneficiary.objects.filter(national_id=data["national_id"]).exists()
              or FamilyMember.objects.filter(national_id=data["national_id"], parent__company=company).exists()):
            form.add_error(None, "سبق تسجيل طلب بهذا الرقم لدى الجمعية، وسيتواصل معك فريقنا. "
                                 "للاستفسار عن طلبك تواصل معنا من صفحة «تواصل معنا».")
        else:
            notes = [APPLY_NOTE, f"عدد أفراد الأسرة: {data['family_size']}",
                     f"تفاصيل الطلب: {data['needs']}"]
            beneficiary = Beneficiary(
                company=company, full_name=data["full_name"], national_id=data["national_id"], phone=data["phone"],
                nationality=data["nationality"] or "سعودي", gender=data["gender"],
                marital_status=data["marital_status"] or "", city=data["city"], district=data["district"],
                category=data["category"], housing_type=data["housing_type"] or "",
                monthly_income=data["monthly_income"] or 0, notes="\n".join(notes))
            try:
                with transaction.atomic():
                    beneficiary.save()
            except Exception:
                logger.exception("website apply failed")
                form.add_error(None, "تعذر حفظ الطلب، حاول مرة أخرى أو تواصل معنا.")
            else:
                done, reference = True, beneficiary.file_number
                _notify(company, f"طلب مساعدة جديد من الموقع: {beneficiary.full_name} ({reference})",
                        erp_url("doc_detail", "beneficiary", beneficiary.pk))
    return render(request, "website/apply.html", {"form": form, "done": done, "reference": reference})


@site_view
def contact(request):
    from office.models import Correspondence, Direction

    form = ContactForm(request.POST or None)
    done, reference = False, None
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        company = request.site.company
        if data["website"]:
            done = True
        else:
            contact_line = " — ".join(v for v in (data["name"], data["phone"], data["email"]) if v)
            item = Correspondence(
                company=company, direction=Direction.INCOMING, subject=f"رسالة من الموقع: {data['subject']}"[:255],
                party=contact_line[:200], external_reference=WEB_SUBMISSION,
                body=f"{data['message']}\n\nالمرسل: {data['name']}\nالجوال: {data['phone'] or '—'}\n"
                     f"البريد: {data['email'] or '—'}")
            with transaction.atomic():
                item.save()
            done, reference = True, item.number
            _notify(company, f"رسالة جديدة من الموقع: {data['subject']}",
                    erp_url("doc_detail", "correspondence", item.pk))
    return render(request, "website/contact.html", {"form": form, "done": done, "reference": reference})


# ---------------------------------------------------------------- الروابط العامة للوحدات على نطاق الموقع

def _token_view(view, model):
    """صفحة التسجيل العامة لبرنامج/فرصة/استبيان الجمعية نفسها عبر نطاق موقعها أو المعاينة."""

    @site_view
    def wrapper(request, token):
        if not model.all_objects.filter(company=request.site.company, public_token=token).exists():
            raise Http404
        return view(request, token)

    return wrapper


def _verify_view(view):
    @site_view
    def wrapper(request, code):
        return view(request, code)

    return wrapper


program_register = _token_view(program_views.public_register, TrainingProgram)
program_verify = _verify_view(program_views.public_verify)
volunteer_register = _token_view(volunteer_views.public_register, VolunteerOpportunity)
volunteer_verify = _verify_view(volunteer_views.public_verify)
survey_respond = _token_view(relations_views.survey_respond, Survey)


# ---------------------------------------------------------------- محركات البحث

@site_view
def robots(request):
    sitemap = request.build_absolute_uri(site_url(request, "site_sitemap"))
    body = f"User-agent: *\nAllow: /\nSitemap: {sitemap}\n"
    if not request.site.is_published:
        body = "User-agent: *\nDisallow: /\n"
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@site_view
def sitemap(request):
    from xml.sax.saxutils import escape

    pages = ["site_home", "site_about", "site_programs", "site_news", "site_gallery", "site_governance",
             "site_apply", "site_contact"]
    urls = [(request.build_absolute_uri(site_url(request, name)), None) for name in pages]
    urls += [(request.build_absolute_uri(site_url(request, "site_news_detail", p.pk)), p.published_on)
             for p in NewsPost.objects.filter(is_published=True).only("pk", "published_on")[:500]]
    urls += [(request.build_absolute_uri(site_url(request, "site_album", a.pk)), a.event_date)
             for a in Album.objects.filter(is_published=True).only("pk", "event_date")[:200]]
    rows = "".join(f"<url><loc>{escape(loc)}</loc>{f'<lastmod>{d.isoformat()}</lastmod>' if d else ''}</url>"
                   for loc, d in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>')
    return HttpResponse(xml, content_type="application/xml; charset=utf-8")
