"""ملفات PDF للمستندات تُولَّد على الخادم من قالب الطباعة نفسه (WeasyPrint)، لتنزيلها أو إرفاقها بالبريد
أو مشاركة رابطها مع العميل في واتساب.

لا يجلب المولّد أي رابط خارجي: الخطوط والأنماط من الملفات الثابتة للنظام، والشعار والرمز (QR) مضمّنان في الصفحة.
"""
import logging
import mimetypes
import os
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.core import signing
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

log = logging.getLogger(__name__)

# المستندات التي يجوز مشاركتها مع العميل برابط عام موقّع (لا يحتاج دخولاً)
SHAREABLE = {"sales-invoice", "quotation", "sales-order", "delivery-note"}
LINK_SALT = "synar.document-pdf"
LINK_MAX_AGE = int(os.environ.get("SYNAR_PDF_LINK_DAYS", "180")) * 86400


class PDFUnavailable(Exception):
    """مكتبة WeasyPrint أو مكتبات النظام التي تحتاجها غير مثبتة."""


def _weasyprint():
    try:
        import weasyprint
    except (ImportError, OSError) as exc:  # OSError: مكتبات Pango غير موجودة على الخادم
        raise PDFUnavailable("توليد PDF غير متاح على هذا الخادم (مكتبة WeasyPrint غير مثبتة).") from exc
    return weasyprint


def _static_file(url):
    """مسار الملف الثابت المحلي لرابط /static/...؛ None لأي رابط آخر."""
    path = unquote(urlparse(url).path)
    prefix = "/" + settings.STATIC_URL.strip("/") + "/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    if ".." in name.split("/"):
        return None
    collected = os.path.join(str(settings.STATIC_ROOT), name)
    if os.path.isfile(collected):
        return collected
    return finders.find(name)


def _fetcher(url, *args, **kwargs):
    weasyprint = _weasyprint()
    if url.startswith("data:"):
        return weasyprint.default_url_fetcher(url, *args, **kwargs)
    found = _static_file(url) if url.startswith("file://") else None
    if not found:
        # لا اتصال بأي عنوان آخر من الخادم (حماية من طلب عناوين داخلية عبر محتوى المستند)
        raise ValueError(f"رابط غير مسموح في ملف PDF: {url[:80]}")
    mime = mimetypes.guess_type(found)[0] or ("text/css" if found.endswith(".css") else "application/octet-stream")
    with open(found, "rb") as fh:
        return {"string": fh.read(), "mime_type": mime, "filename": found}


def _tint(color, percent):
    """لون فاتح من لون الهوية (بديل color-mix غير المدعوم في مولد PDF)."""
    color = (color or "#1f4e79").lstrip("#")
    try:
        rgb = [int(color[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        rgb = [31, 78, 121]
    mixed = [round(c * percent / 100 + 255 * (1 - percent / 100)) for c in rgb]
    return "#" + "".join(f"{c:02x}" for c in mixed)


def render_pdf(dt, obj, company):
    """يعيد محتوى ملف PDF للمستند بالقالب المختار في إعدادات الطباعة."""
    from core.tenancy import tenant_context
    from core.views import print_page_context

    weasyprint = _weasyprint()
    with tenant_context(company):
        context = print_page_context(dt, obj, company)
        accent = context["ps"].accent_color
        context.update(pdf=True, tints={p: _tint(accent, p) for p in (4, 6, 12, 25)})
        html = render_to_string("core/print.html", context)
    return weasyprint.HTML(string=html, base_url="file:///", url_fetcher=_fetcher).write_pdf()


def filename(obj):
    return f"{obj}".replace("/", "-").replace(" ", "_") + ".pdf"


def pdf_response(content, obj, inline=False):
    response = HttpResponse(content, content_type="application/pdf")
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{filename(obj)}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------- الروابط العامة الموقعة ----------------

def share_token(company, slug, obj):
    return signing.dumps({"c": company.pk, "s": slug, "p": obj.pk}, salt=LINK_SALT, compress=True)


def share_path(company, slug, obj):
    """مسار رابط PDF عام للعميل، أو "" إن لم يكن المستند قابلاً للمشاركة."""
    if slug not in SHAREABLE or getattr(obj, "is_draft", False):
        return ""
    return reverse("document_pdf_public", args=[share_token(company, slug, obj)])


def share_url(request, company, slug, obj):
    path = share_path(company, slug, obj)
    if not path:
        return ""
    if request is not None:
        return request.build_absolute_uri(path)
    from core.mail import active_config

    config = active_config(company)
    base = (config.site_url if config else "") or settings.SYNAR_SITE_URL
    return f"{base.rstrip('/')}{path}" if base else ""


def public_pdf(request, token):
    """ملف PDF لمستند شاركته المنشأة مع عميلها؛ الرابط موقّع ولا يحتاج دخولاً."""
    from core.models import Company
    from core.registry import get_doctype
    from core.tenancy import tenant_context

    try:
        data = signing.loads(token, salt=LINK_SALT, max_age=LINK_MAX_AGE)
    except signing.BadSignature:
        raise Http404("الرابط غير صحيح أو انتهت صلاحيته.")
    if data.get("s") not in SHAREABLE:
        raise Http404
    company = get_object_or_404(Company, pk=data.get("c"))
    dt = get_doctype(data["s"])
    with tenant_context(company):
        obj = get_object_or_404(dt.model.objects.all(), pk=data.get("p"))
        if getattr(obj, "is_draft", False):
            raise Http404
    try:
        content = render_pdf(dt, obj, company)
    except PDFUnavailable as exc:
        return HttpResponse(str(exc), status=503, content_type="text/plain; charset=utf-8")
    return pdf_response(content, obj, inline=True)


# ---------------- داخل النظام ----------------

def _document(request, slug, pk):
    from core.registry import get_doctype
    from core.views import _scoped

    dt = get_doctype(slug)
    return dt, get_object_or_404(_scoped(request, dt), pk=pk)


@login_required
def doc_pdf(request, slug, pk):
    """تنزيل المستند PDF بصلاحيات المستخدم على المستند نفسه."""
    from django.contrib import messages
    from django.shortcuts import redirect

    dt, obj = _document(request, slug, pk)
    try:
        content = render_pdf(dt, obj, request.company)
    except PDFUnavailable as exc:
        messages.error(request, str(exc))
        return redirect(obj.get_absolute_url())
    return pdf_response(content, obj)


def _party_email(obj):
    party = getattr(obj, "customer", None) if hasattr(obj, "customer_id") else getattr(obj, "supplier", None)
    return getattr(party, "email", "") if party is not None else ""


@login_required
@require_POST
def doc_email(request, slug, pk):
    """إرسال المستند PDF مرفقاً بالبريد إلى العميل أو المورد (أو أي بريد يُدخله المستخدم)."""
    from django.contrib import messages
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.shortcuts import redirect

    from core.mail import mail_enabled, send

    dt, obj = _document(request, slug, pk)
    back = redirect(obj.get_absolute_url())
    if not mail_enabled(request.company):
        messages.error(request, "البريد الإلكتروني غير مفعّل. اضبطه من الإعدادات ← البريد الإلكتروني.")
        return back
    to = (request.POST.get("to") or _party_email(obj)).strip()
    try:
        validate_email(to)
    except ValidationError:
        messages.error(request, "لا يوجد بريد إلكتروني صحيح للمستلم. أضفه في بطاقة العميل أو المورد.")
        return back
    try:
        content = render_pdf(dt, obj, request.company)
    except PDFUnavailable as exc:
        messages.error(request, str(exc))
        return back
    title = getattr(obj, "document_title", "") or dt.label
    company = request.company
    body = (f"السلام عليكم،\n\nمرفق {title} رقم {obj} من {company.name}.\n"
            + (f"الإجمالي: {obj.grand_total:,.2f} {company.currency.code}\n" if hasattr(obj, "grand_total") else "")
            + f"\nمع التحية،\n{company.name}")
    sent = send(company, f"{title} {obj} — {company.name}", body, [to],
                attachments=[(filename(obj), content, "application/pdf")], prefix=False)
    if sent:
        messages.success(request, f"أُرسل {title} {obj} إلى {to}.")
    else:
        messages.error(request, "تعذر إرسال البريد.")
    return back
