"""إعدادات موقع الجمعية وإدارة محتواه من داخل النظام."""
import functools
import operator

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from website.forms import AlbumPhotosForm, DocumentUploadForm, ImageUploadForm, WebsiteForm
from website.models import (APPLY_NOTE, FAQ, FEEDBACK_PREFIXES, MEMBERSHIP_NOTE, WEB_SUBMISSION, Album, AlbumPhoto,
                            HeroSlide, ImpactCounter, NewsletterSubscriber, NewsPost, Partner, PublishedDocument, Video,
                            Website)
from website.site import clear_cache


@login_required
def website_settings(request):
    site = Website.for_company(request.company)
    form = WebsiteForm(request.POST or None, request.FILES or None, instance=site)
    if request.method == "POST" and form.is_valid():
        form.save()
        clear_cache()
        messages.success(request, "حُفظت إعدادات الموقع.")
        return redirect("website_settings")
    return render(request, "website/settings.html", {"title": "إعدادات الموقع الإلكتروني", "form": form, "site": site})


# ---------------------------------------------------------------- إدارة محتوى الموقع


def _can_write(request, slug):
    membership = getattr(request, "membership", None)
    return membership is None or membership.doctype_access(slug) == "write"


@login_required
def website_dashboard(request):
    from beneficiaries.models import Beneficiary
    from governance.models import Member
    from office.models import Correspondence, Direction

    site = Website.for_company(request.company)
    month_start = timezone.localdate().replace(day=1)
    stats = [
        ("الأخبار المنشورة", NewsPost.objects.filter(is_published=True).count(), "news-post"),
        ("وثائق الحوكمة والإفصاح", PublishedDocument.objects.filter(is_published=True).count(), "published-document"),
        ("ألبومات الصور", Album.objects.count(), "album"),
        ("شركاء النجاح", Partner.objects.filter(is_published=True).count(), "partner"),
        ("شرائح الواجهة", HeroSlide.objects.filter(is_published=True).count(), "hero-slide"),
        ("أرقام الأثر", ImpactCounter.objects.filter(is_published=True).count(), "impact-counter"),
        ("الفيديوهات", Video.objects.filter(is_published=True).count(), "video"),
        ("الأسئلة الشائعة", FAQ.objects.filter(is_published=True).count(), "faq"),
        ("مشتركو النشرة البريدية", NewsletterSubscriber.objects.filter(is_active=True).count(),
         "newsletter-subscriber"),
    ]
    web_mail = Correspondence.objects.filter(direction=Direction.INCOMING, external_reference=WEB_SUBMISSION,
                                             date__gte=month_start)
    feedback = functools.reduce(operator.or_, (Q(subject__startswith=p) for p in FEEDBACK_PREFIXES))
    inbound = [
        ("طلبات المساعدة من الموقع هذا الشهر", Beneficiary.objects.filter(
            notes__startswith=APPLY_NOTE, registered_on__gte=month_start).count(), "beneficiary"),
        ("رسائل «تواصل معنا» هذا الشهر", web_mail.exclude(feedback).count(), "correspondence"),
        ("طلبات العضوية من الموقع هذا الشهر", Member.objects.filter(
            notes__startswith=MEMBERSHIP_NOTE, join_date__gte=month_start).count(), "member"),
        ("الشكاوى والمقترحات هذا الشهر", web_mail.filter(feedback).count(), "correspondence"),
    ]
    return render(request, "website/dashboard.html", {
        "title": "إدارة الموقع الإلكتروني", "site": site, "stats": stats, "inbound": inbound,
        "latest_news": NewsPost.objects.all()[:5],
    })


@login_required
def album_photos(request, pk):
    album = get_object_or_404(Album, pk=pk)
    form = AlbumPhotosForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        if not _can_write(request, "album"):
            messages.error(request, "صلاحيتك على ألبومات الصور للاطلاع فقط.")
            return redirect("website_album_photos", pk=album.pk)
        if form.is_valid():
            start = (album.photos.order_by("-sort").values_list("sort", flat=True).first() or 0) + 1
            caption = form.cleaned_data["caption"]
            AlbumPhoto.objects.bulk_create([AlbumPhoto(album=album, image=image, caption=caption, sort=start + i)
                                            for i, image in enumerate(form.cleaned_data["photos"])])
            messages.success(request, f"تم رفع {len(form.cleaned_data['photos'])} صورة إلى الألبوم.")
            return redirect("website_album_photos", pk=album.pk)
    return render(request, "website/album_photos.html", {
        "title": f"صور الألبوم: {album.title}", "album": album, "form": form, "photos": album.photos.all(),
    })


@login_required
@require_POST
def album_photo_delete(request, pk, photo_pk):
    album = get_object_or_404(Album, pk=pk)
    membership = getattr(request, "membership", None)
    if not _can_write(request, "album") or (membership is not None and not membership.may("delete")):
        messages.error(request, "ليست لديك صلاحية حذف الصور.")
    else:
        get_object_or_404(AlbumPhoto, pk=photo_pk, album=album).delete()
        messages.success(request, "تم حذف الصورة.")
    return redirect("website_album_photos", pk=album.pk)


# نوع الرفع ← (النموذج، نوع المستند، الحقل)
UPLOADS = {
    "news": (NewsPost, "news-post", "cover"),
    "partner": (Partner, "partner", "logo"),
    "slide": (HeroSlide, "hero-slide", "image"),
    "document": (PublishedDocument, "published-document", None),
}


@login_required
@require_POST
def upload(request, kind, pk):
    """رفع صورة غلاف الخبر أو شعار الشريك أو ملف الوثيقة من صفحة المستند."""
    if kind not in UPLOADS:
        raise Http404
    model, slug, field = UPLOADS[kind]
    obj = get_object_or_404(model, pk=pk)
    if not _can_write(request, slug):
        messages.error(request, "صلاحيتك على هذا المستند للاطلاع فقط.")
        return redirect(obj.get_absolute_url())
    if field is None:
        form = DocumentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            obj.file_name, obj.content_type, obj.content, obj.size = f["name"], f["content_type"], f["data"], len(f["data"])
            obj.save(update_fields=["file_name", "content_type", "content", "size"])
            messages.success(request, "تم رفع ملف الوثيقة.")
    else:
        form = ImageUploadForm(request.POST, request.FILES)
        if form.is_valid() and (form.cleaned_data["file"] or form.cleaned_data["remove"]):
            setattr(obj, field, form.cleaned_data["file"])
            obj.save(update_fields=[field])
            messages.success(request, "تم حفظ الصورة." if form.cleaned_data["file"] else "تم حذف الصورة.")
        elif form.is_valid():
            messages.error(request, "اختر صورة لرفعها.")
    if not form.is_valid():
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    return redirect(obj.get_absolute_url())
