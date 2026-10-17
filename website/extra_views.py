"""صفحات الموقع الإضافية: الأسئلة الشائعة، الفيديوهات، طلب العضوية، الشكاوى والمقترحات، التحقق من الشهادات،
سياسة الخصوصية، الخدمات الإلكترونية، والاشتراك في النشرة البريدية."""
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from website.forms import FeedbackForm, MembershipForm, NewsletterForm, VerifyForm
from website.models import (FAQ, FEEDBACK_TYPES, MEMBERSHIP_NOTE, WEB_SUBMISSION, NewsletterSubscriber, Video,
                            youtube_id)
from website.public_views import _notify, erp_url, open_surveys
from website.ratelimit import LIMIT_MESSAGE, too_many
from website.site import site_url, site_view

logger = logging.getLogger(__name__)
GENERAL_FAQ = "أسئلة عامة"


@site_view
def faq(request):
    groups = {}
    for item in FAQ.objects.filter(is_published=True):
        groups.setdefault(item.category.strip() or GENERAL_FAQ, []).append(item)
    return render(request, "website/faq.html", {"groups": list(groups.items())})


@site_view
def videos(request):
    items = [v for v in Video.objects.filter(is_published=True)[:60] if v.youtube_id]
    intro_id = youtube_id(request.site.video_intro_url)
    return render(request, "website/videos.html", {
        "videos": items, "intro_embed": f"https://www.youtube-nocookie.com/embed/{intro_id}" if intro_id else "",
    })


@site_view
def membership(request):
    from governance.models import Member, MemberStatus

    site = request.site
    if not site.allow_membership_requests:
        raise Http404
    form = MembershipForm(request.POST or None)
    done, reference = False, None
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        company = site.company
        existing = Member.objects.filter(national_id=data["national_id"]).exclude(status=MemberStatus.ENDED).first()
        if data["website"]:
            done = True  # بوت: لا نحفظ شيئاً
        elif too_many(request, "membership", 5):
            form.add_error(None, LIMIT_MESSAGE)
        elif existing is not None:
            form.add_error(None, "طلبك بهذا الرقم قيد الدراسة لدى الجمعية، وسنتواصل معك."
                           if existing.status == MemberStatus.PENDING else
                           "صاحب هذا الرقم عضو في الجمعية العمومية. للاستفسار تواصل معنا من صفحة «تواصل معنا».")
        else:
            notes = MEMBERSHIP_NOTE + (f"\n{data['notes']}" if data["notes"] else "")
            member = Member(company=company, full_name=data["full_name"], national_id=data["national_id"],
                            phone=data["phone"], email=data["email"], category=data["category"],
                            status=MemberStatus.PENDING, notes=notes)
            try:
                with transaction.atomic():
                    member.save()
            except Exception:
                logger.exception("website membership request failed")
                form.add_error(None, "تعذر حفظ الطلب، حاول مرة أخرى أو تواصل معنا.")
            else:
                done, reference = True, member.member_number
                _notify(company, f"طلب عضوية جديد من الموقع: {member.full_name} ({reference})",
                        erp_url("doc_detail", "member", member.pk))
    return render(request, "website/membership.html", {"form": form, "done": done, "reference": reference})


@site_view
def feedback(request):
    from office.models import Correspondence, Direction

    form = FeedbackForm(request.POST or None)
    done, reference = False, None
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        company = request.site.company
        if data["website"]:
            done = True
        elif too_many(request, "feedback", 5):
            form.add_error(None, LIMIT_MESSAGE)
        else:
            kind = dict(FEEDBACK_TYPES)[data["kind"]]
            contact_line = " — ".join(v for v in (data["name"], data["phone"], data["email"]) if v) or "زائر الموقع"
            item = Correspondence(
                company=company, direction=Direction.INCOMING, subject=f"{kind} من الموقع: {data['subject']}"[:255],
                party=contact_line[:200], external_reference=WEB_SUBMISSION,
                body=f"النوع: {kind}\n\n{data['message']}\n\nالمرسل: {data['name'] or '—'}\n"
                     f"الجوال: {data['phone'] or '—'}\nالبريد: {data['email'] or '—'}")
            with transaction.atomic():
                item.save()
            done, reference = True, item.number
            _notify(company, f"{kind} من الموقع: {data['subject']}", erp_url("doc_detail", "correspondence", item.pk))
    return render(request, "website/feedback.html", {"form": form, "done": done, "reference": reference})


def find_certificate(request, code):
    """يبحث عن الرمز في شهادات التدريب والتطوع وشهادات التبرع لهذه الجمعية فقط.

    يعيد (رابط التحويل، طلب التبرع) — أحدهما أو لا شيء."""
    from programs.models import EnrollmentStatus, ProgramEnrollment
    from store.models import DonationOrder, OrderStatus
    from volunteering.models import ParticipationStatus, VolunteerParticipation

    company = request.site.company
    if ProgramEnrollment.all_objects.filter(company=company, certificate_code=code,
                                            status=EnrollmentStatus.PASSED).exists():
        return site_url(request, "program_verify", code), None
    if VolunteerParticipation.all_objects.filter(company=company, certificate_code=code,
                                                 status=ParticipationStatus.COMPLETED).exists():
        return site_url(request, "volunteer_verify", code), None
    order = DonationOrder.all_objects.filter(company=company, verify_code=code, status=OrderStatus.PAID).first()
    return None, order


@site_view
def verify(request):
    form = VerifyForm(request.GET if "code" in request.GET else None)
    order, not_found, limited = None, False, False
    if form.is_bound and form.is_valid():
        code = form.cleaned_data["code"]
        if too_many(request, "verify", 30):
            limited = True
            form.add_error(None, LIMIT_MESSAGE)
        else:
            url, order = find_certificate(request, code)
            if url:
                return redirect(url)
            not_found = order is None
    certificate_url = ""
    if order is not None:
        certificate_url = site_url(request, "store_certificate", order.number) + f"?c={order.verify_code}"
    return render(request, "website/verify.html", {
        "form": form, "order": order, "certificate_url": certificate_url, "not_found": not_found,
        "limited": limited,
    })


@site_view
def privacy(request):
    return render(request, "website/privacy.html", {"policy": request.site.privacy_policy.strip()})


@site_view
def services(request):
    site = request.site
    tiles = [
        {"icon": "aid", "title": "طلب مساعدة", "url": site_url(request, "site_apply"),
         "description": "قدّم طلب مساعدة أو سجّل أسرتك لدى الجمعية لدراسة حالتها."},
        {"icon": "volunteer", "title": "التطوع", "url": site_url(request, "site_programs") + "#volunteering",
         "description": "سجّل في الفرص التطوعية المتاحة واحصل على شهادة بساعاتك."},
    ]
    if site.specialty_registration_available:
        tiles.insert(0, {"icon": "member", "title": site.specialty_registration_label,
                         "url": site_url(request, "site_specialty_register"),
                         "description": "قدّم طلبك إلكترونياً لتراجعه الجمعية وتتواصل معك بشأن القبول والخدمة المناسبة."})
    if site.allow_membership_requests:
        tiles.append({"icon": "member", "title": "طلب العضوية", "url": site_url(request, "site_membership"),
                      "description": "انضم إلى الجمعية العمومية وشارك في مسيرة الجمعية."})
    tiles += [
        {"icon": "donate", "title": "التبرع الإلكتروني", "url": site_url(request, "store_home"),
         "description": "تبرّع بأمان وبسهولة لبرامج الجمعية ومشاريعها."},
        {"icon": "verify", "title": "التحقق من الشهادات", "url": site_url(request, "site_verify"),
         "description": "تحقّق من صحة شهادة تدريب أو تطوع أو تبرع صادرة من الجمعية."},
        {"icon": "feedback", "title": "الشكاوى والمقترحات", "url": site_url(request, "site_feedback"),
         "description": "شاركنا ملاحظتك أو اقتراحك أو شكواك لنطوّر خدماتنا."},
        {"icon": "contact", "title": "تواصل معنا", "url": site_url(request, "site_contact"),
         "description": "راسلنا أو تواصل معنا عبر الهاتف والبريد ووسائل التواصل."},
    ]
    for survey in open_surveys(request, 4):
        tiles.append({"icon": "survey", "title": survey.title, "url": survey.public_link,
                      "description": "استبيان مفتوح: شاركنا رأيك لتحسين خدماتنا."})
    return render(request, "website/services.html", {"services": tiles})


def _with_query(url, **params):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query += [(k, str(v)) for k, v in params.items()]
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def safe_next(request):
    """مسار العودة بعد الاشتراك: مسار نسبي داخل الموقع فقط، وإلا الصفحة الرئيسية."""
    target = (request.POST.get("next") or "").strip()
    if (target.startswith("/") and not target.startswith("//") and "\\" not in target
            and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                require_https=request.is_secure())):
        return target
    return site_url(request, "site_home")


@site_view
@require_POST
def newsletter(request):
    form = NewsletterForm(request.POST)
    ok = False
    if form.is_valid():
        ok = True
        if not form.cleaned_data["website"] and not too_many(request, "newsletter", 5):
            email = form.cleaned_data["email"].strip().lower()
            subscriber, created = NewsletterSubscriber.objects.get_or_create(
                company=request.site.company, email=email)
            if not created and not subscriber.is_active:
                subscriber.is_active = True
                subscriber.save(update_fields=["is_active"])
        elif not form.cleaned_data["website"]:
            ok = False  # تجاوز حد الطلبات
    return redirect(_with_query(safe_next(request), subscribed=1 if ok else 0))
