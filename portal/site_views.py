"""بوابة المستفيد على موقع الجمعية: دخول رب الأسرة برقم الهوية والجوال ورمز تحقق، ثم عرض ملفه وطلباته.

كل استعلام مقيد بالمستفيد المسجل دخوله وبالجمعية صاحبة الموقع، ولا تُعرض الملاحظات الداخلية أو الدرجات أو أسماء الموظفين.
"""
import functools
import hashlib
import hmac
import logging
import secrets
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from beneficiaries.models import AidDisbursement, AidType, Beneficiary, BeneficiaryStatus, CaseAssessment
from core.models import Attachment
from portal.forms import (MAX_FILES, CodeForm, LoginForm, ProfileForm, ServiceRequestForm, check_upload)
from portal.models import PortalLogin, ProfileUpdateRequest, ServiceRequest, ServiceStatus, UpdateStatus
from store.service import phone_variants
from website.ratelimit import LIMIT_MESSAGE, too_many
from website.site import site_url, site_view

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
MAX_OPEN_REQUESTS = 5
MAX_PORTAL_DOCUMENTS = 50
PORTAL_MARK = "مرفوع من بوابة المستفيد"
SENT_MESSAGE = "إن كانت البيانات مطابقة لملف مسجل لدى الجمعية فسيصل إلى جوالك المسجل رمز تحقق من 6 أرقام."
BAD_CODE = "رمز التحقق غير صحيح أو منتهي الصلاحية."
TOO_MANY_ATTEMPTS = "تجاوزت عدد المحاولات. اطلب رمزاً جديداً."


# ─────────────────────────── الجلسة ───────────────────────────

def _key(request, name):
    return f"portal_{name}_{request.site.company_id}"


def _otp_hash(company_id, beneficiary_id, code):
    return hashlib.sha256(
        f"{django_settings.SECRET_KEY}:portal:{company_id}:{beneficiary_id}:{code}".encode()).hexdigest()


def _current(request):
    """المستفيد المسجل دخوله في هذه الجمعية، أو None (يُعاد التحقق منه في كل طلب)."""
    pk = request.session.get(_key(request, "ben"))
    if not pk:
        return None
    return (Beneficiary.objects.filter(pk=pk, company=request.site.company)
            .exclude(status=BeneficiaryStatus.CLOSED).select_related("category").first())


def portal_page(view):
    """صفحة داخل البوابة: تتطلب دخول المستفيد، وتمرر ملفه في request.beneficiary."""

    @site_view
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        beneficiary = _current(request)
        if beneficiary is None:
            request.session.pop(_key(request, "ben"), None)
            return redirect(site_url(request, "portal_login"))
        request.beneficiary = beneficiary
        return view(request, *args, **kwargs)

    return wrapper


def _render(request, template, active, **context):
    context.update(active=active, beneficiary=getattr(request, "beneficiary", None), tabs=[
        ("portal_home", "ملفي", "user"), ("portal_aid", "المساعدات", "aid"),
        ("portal_requests", "طلباتي", "doc"), ("portal_appointments", "مواعيدي", "calendar"), ("portal_profile", "تحديث بياناتي", "contact"),
        ("portal_documents", "المستندات", "copy"), ("portal_programs", "برامجي ومتابعتي", "book")])
    return render(request, template, context)


def _notify(request, title, slug, pk):
    from website.public_views import _notify as notify, erp_url

    notify(request.site.company, title, erp_url("doc_detail", slug, pk))


# ─────────────────────────── الدخول ───────────────────────────

def _find_beneficiary(company, national_id, phone):
    """ملف رب الأسرة المطابق للهوية والجوال معاً في هذه الجمعية، أو None."""
    return (Beneficiary.objects.filter(company=company, national_id=national_id, phone__in=phone_variants(phone))
            .exclude(status=BeneficiaryStatus.CLOSED).first())


def _send_code(company, beneficiary):
    """يرسل رمزاً جديداً إن لم يُرسل له رمز خلال الدقيقة الماضية. لا يكشف للزائر شيئاً عن النتيجة."""
    last = PortalLogin.objects.filter(company=company, beneficiary=beneficiary).order_by("-sent_at").first()
    if last and (timezone.now() - last.sent_at).total_seconds() < OTP_RESEND_SECONDS:
        return
    code = f"{secrets.randbelow(10 ** 6):06d}"
    PortalLogin.objects.filter(company=company, beneficiary=beneficiary).delete()
    PortalLogin.objects.create(company=company, beneficiary=beneficiary,
                               code_hash=_otp_hash(company.pk, beneficiary.pk, code))
    try:
        from messaging.service import send

        send(company, beneficiary.phone, f"رمز الدخول لبوابة المستفيد لدى {company.name}: {code}\n"
                                         f"صالح {OTP_TTL_MINUTES} دقائق. لا تشاركه مع أحد.",
             event="portal_otp", reference="portal-login", mode="provider")
    except Exception:  # noqa: BLE001 — الرد للزائر واحد في كل الأحوال
        logger.exception("portal otp send failed")


def _verify(request, company, code):
    """يعيد (المستفيد، الخطأ)."""
    pending = request.session.get(_key(request, "otp")) or {}
    attempts = int(pending.get("attempts", 0))
    if attempts >= OTP_MAX_ATTEMPTS:
        return None, TOO_MANY_ATTEMPTS
    # الرمز مرتبط بالهوية والجوال معاً كما أُدخلا عند طلبه
    login = (PortalLogin.objects.filter(company=company, beneficiary__national_id=pending.get("nid", ""),
                                        beneficiary__phone__in=phone_variants(pending.get("phone", "")),
                                        beneficiary__company=company)
             .select_related("beneficiary").order_by("-sent_at").first())
    if login is not None and login.attempts >= OTP_MAX_ATTEMPTS:
        return None, TOO_MANY_ATTEMPTS
    valid = (login is not None
             and timezone.now() - login.sent_at <= timezone.timedelta(minutes=OTP_TTL_MINUTES)
             and hmac.compare_digest(_otp_hash(company.pk, login.beneficiary_id, code), login.code_hash))
    if not valid:
        pending["attempts"] = attempts + 1
        request.session[_key(request, "otp")] = pending
        if login is not None:
            PortalLogin.objects.filter(pk=login.pk).update(attempts=login.attempts + 1)
        return None, BAD_CODE
    PortalLogin.objects.filter(company=company, beneficiary=login.beneficiary).delete()
    return login.beneficiary, ""


@site_view
def portal_login(request):
    company = request.site.company
    if _current(request) is not None:
        return redirect(site_url(request, "portal_home"))
    message = error = ""
    pending = request.session.get(_key(request, "otp")) or {}
    login_form, code_form = LoginForm(), CodeForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if request.POST.get("website"):
            return redirect(site_url(request, "portal_login"))
        if action == "send":
            login_form = LoginForm(request.POST)
            if login_form.is_valid():
                sent_at = float(pending.get("at", 0))
                if pending and timezone.now().timestamp() - sent_at < OTP_RESEND_SECONDS:
                    error = "انتظر دقيقة قبل طلب رمز جديد."
                elif too_many(request, "portal_otp", 10):
                    error = LIMIT_MESSAGE
                else:
                    data = login_form.cleaned_data
                    beneficiary = _find_beneficiary(company, data["national_id"], data["phone"])
                    if beneficiary is not None:
                        _send_code(company, beneficiary)
                    pending = {"nid": data["national_id"], "phone": data["phone"], "attempts": 0, "at": timezone.now().timestamp()}
                    request.session[_key(request, "otp")] = pending
                    message = SENT_MESSAGE
        elif action == "verify" and pending:
            code_form = CodeForm(request.POST)
            if code_form.is_valid():
                beneficiary, error = _verify(request, company, code_form.cleaned_data["code"])
                if beneficiary is not None:
                    request.session.cycle_key()
                    request.session.pop(_key(request, "otp"), None)
                    request.session[_key(request, "ben")] = beneficiary.pk
                    return redirect(site_url(request, "portal_home"))
        elif action == "change":
            request.session.pop(_key(request, "otp"), None)
            return redirect(site_url(request, "portal_login"))
    return _render(request, "portal/login.html", "", login_form=login_form, code_form=code_form,
                   pending=bool(pending), message=message, error=error)


@site_view
def portal_logout(request):
    if request.method == "POST":
        request.session.pop(_key(request, "ben"), None)
        request.session.pop(_key(request, "otp"), None)
        request.session.cycle_key()
    return redirect(site_url(request, "portal_login"))


# ─────────────────────────── الصفحات ───────────────────────────

def _aid_qs(request):
    return AidDisbursement.objects.filter(company=request.site.company, beneficiary=request.beneficiary,
                                          docstatus=1)


def _requests_qs(request):
    return ServiceRequest.objects.filter(company=request.site.company, beneficiary=request.beneficiary)


def _updates_qs(request):
    return ProfileUpdateRequest.objects.filter(company=request.site.company, beneficiary=request.beneficiary)


def _documents_qs(request):
    return Attachment.objects.filter(company=request.site.company, doctype=Beneficiary.doctype_slug,
                                     object_id=request.beneficiary.pk, uploaded_by__isnull=True,
                                     description__startswith=PORTAL_MARK).defer("content")


def _year_totals(request):
    year = timezone.localdate().year
    qs = _aid_qs(request).filter(posting_date__year=year)
    cash = qs.filter(aid_type=AidType.CASH).aggregate(t=Sum("amount"))["t"] or Decimal(0)
    return {"year": year, "count": qs.count(), "cash": cash,
            "in_kind": qs.filter(aid_type=AidType.IN_KIND).count()}


@portal_page
def portal_home(request):
    b = request.beneficiary
    last = (CaseAssessment.objects.filter(company=request.site.company, beneficiary=b, docstatus=1)
            .order_by("-posting_date", "-id").only("posting_date", "is_eligible").first())
    members = [(m.full_name, m.get_relation_display()) for m in b.members.all()]
    return _render(request, "portal/home.html", "portal_home", members=members, last_study=last,
                   totals=_year_totals(request),
                   open_requests=_requests_qs(request).filter(
                       status__in=[ServiceStatus.NEW, ServiceStatus.IN_PROGRESS]).count(),
                   pending_update=_updates_qs(request).filter(status=UpdateStatus.PENDING).exists())


@portal_page
def portal_aid(request):
    rows = []
    for d in _aid_qs(request).select_related("program").prefetch_related("items__item__uom").order_by(
            "-posting_date", "-id"):
        rows.append({"date": d.posting_date, "program": d.program.name, "type": d.get_aid_type_display(),
                     "is_cash": d.aid_type == AidType.CASH, "amount": d.amount, "received": bool(d.received_at),
                     "items": "، ".join(f"{i.item.name} × {i.qty.normalize():f} {i.item.uom}" for i in d.items.all())})
    return _render(request, "portal/aid.html", "portal_aid", rows=rows, totals=_year_totals(request))


@portal_page
def portal_requests(request):
    form = ServiceRequestForm(request.POST or None)
    done = ""
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["website"]:
            return redirect(site_url(request, "portal_requests"))
        if too_many(request, "portal_request", 10):
            form.add_error(None, LIMIT_MESSAGE)
        elif _requests_qs(request).filter(status__in=[ServiceStatus.NEW, ServiceStatus.IN_PROGRESS]).count() \
                >= MAX_OPEN_REQUESTS:
            form.add_error(None, f"لديك {MAX_OPEN_REQUESTS} طلبات مفتوحة. انتظر الرد عليها قبل تقديم طلب جديد.")
        else:
            with transaction.atomic():
                item = ServiceRequest.objects.create(company=request.site.company, beneficiary=request.beneficiary,
                                                     kind=data["kind"], description=data["description"].strip())
            _notify(request, f"طلب {item.get_kind_display()} من بوابة المستفيد: {request.beneficiary.full_name} "
                             f"({item.number})", "portal-service-request", item.pk)
            done = item.number
            form = ServiceRequestForm()
    return _render(request, "portal/requests.html", "portal_requests", form=form, done=done,
                   requests=_requests_qs(request), updates=_updates_qs(request))


@portal_page
def portal_profile(request):
    b = request.beneficiary
    pending = _updates_qs(request).filter(status=UpdateStatus.PENDING).first()
    initial = {"phone": b.phone, "city": b.city, "district": b.district, "housing_type": b.housing_type}
    form = ProfileForm(request.POST or None, initial=initial)
    done = ""
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["website"]:
            return redirect(site_url(request, "portal_profile"))
        changed = {k: data[k] for k in ("phone", "city", "district", "housing_type")
                   if data[k] and data[k] != getattr(b, k)}
        if pending is not None:
            form.add_error(None, f"لديك طلب تحديث ({pending.number}) قيد المراجعة. انتظر البت فيه.")
        elif too_many(request, "portal_profile", 5):
            form.add_error(None, LIMIT_MESSAGE)
        elif not changed:
            form.add_error(None, "لم تغيّر أي بيان.")
        else:
            item = ProfileUpdateRequest.objects.create(company=request.site.company, beneficiary=b,
                                                       note=data["note"].strip(), **changed)
            _notify(request, f"طلب تحديث بيانات من بوابة المستفيد: {b.full_name} ({item.number})",
                    "portal-profile-update", item.pk)
            done, pending = item.number, item
    return _render(request, "portal/profile.html", "portal_profile", form=form, done=done, pending=pending)


@portal_page
def portal_documents(request):
    error = done = ""
    if request.method == "POST":
        files = request.FILES.getlist("files")
        description = (request.POST.get("description") or "").strip()[:150]
        existing = _documents_qs(request).count()
        if request.POST.get("website"):
            return redirect(site_url(request, "portal_documents"))
        if not files:
            error = "اختر ملفاً واحداً على الأقل."
        elif len(files) > MAX_FILES:
            error = f"الحد الأقصى {MAX_FILES} ملفات في المرة الواحدة."
        elif existing + len(files) > MAX_PORTAL_DOCUMENTS:
            error = "بلغت الحد الأقصى للمستندات المرفوعة. تواصل مع الجمعية."
        elif too_many(request, "portal_upload", 20):
            error = LIMIT_MESSAGE
        else:
            checked = []
            for f in files:
                content_type, problem = check_upload(f)
                if problem:
                    error = problem
                    break
                checked.append((f, content_type))
            if not error:
                label = f"{PORTAL_MARK}: {description}" if description else PORTAL_MARK
                with transaction.atomic():
                    for f, content_type in checked:
                        Attachment.objects.create(
                            company=request.site.company, doctype=Beneficiary.doctype_slug,
                            object_id=request.beneficiary.pk, file_name=f.name[:255], content_type=content_type,
                            size=f.size, content=f.read(), description=label[:255])
                _notify(request, f"مستندات جديدة من بوابة المستفيد: {request.beneficiary.full_name} "
                                 f"({len(checked)} ملف)", Beneficiary.doctype_slug, request.beneficiary.pk)
                done = f"تم رفع {len(checked)} ملف، وسيراجعها فريق الجمعية."
    return _render(request, "portal/documents.html", "portal_documents", documents=_documents_qs(request),
                   error=error, done=done, max_files=MAX_FILES)


@portal_page
def portal_programs(request):
    from programs.models import ProgramEnrollment,QuranLearningPlan,QuranProgress,DawahFollowUp,UmrahParticipant,HealthAppointment
    company=request.site.company;beneficiary=request.beneficiary
    enrollments=ProgramEnrollment.objects.filter(company=company,beneficiary=beneficiary).select_related("program")[:50]
    plans=[];progress=[];followups=[];trips=[];appointments=[]
    if company.association_specialty=="quran":
        plans=QuranLearningPlan.objects.filter(company=company,student__beneficiary=beneficiary,active=True).select_related("student")[:30]
        progress=QuranProgress.objects.filter(company=company,student__beneficiary=beneficiary).select_related("student").order_by("-date")[:30]
    elif company.association_specialty=="dawah":
        followups=DawahFollowUp.objects.filter(company=company,person__beneficiary=beneficiary).select_related("person")[:30]
        trips=UmrahParticipant.objects.filter(company=company,participant__beneficiary=beneficiary).select_related("trip")[:30]
    elif company.association_specialty=="health":
        appointments=HealthAppointment.objects.filter(company=company,patient__beneficiary=beneficiary,patient__portal_visible=True).select_related("patient").order_by("-scheduled_at")[:30]
    return _render(request,"portal/programs.html","portal_programs",enrollments=enrollments,plans=plans,progress=progress,followups=followups,trips=trips,appointments=appointments)
