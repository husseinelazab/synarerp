"""مرفقات المستندات: الرفع والتنزيل والحذف بصلاحيات المستند نفسه (الوحدة، الفرع، والاطلاع فقط)."""
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.models import MAX_ATTACHMENT_BYTES, Attachment
from core.registry import get_doctype


def _document(request, slug, pk):
    from core.views import _scoped

    dt = get_doctype(slug)
    return dt, get_object_or_404(_scoped(request, dt), pk=pk)


def _can_write(request, slug):
    membership = getattr(request, "membership", None)
    return membership is None or membership.doctype_access(slug) == "write"


@login_required
@require_POST
def upload(request, slug, pk):
    dt, obj = _document(request, slug, pk)
    files = request.FILES.getlist("files")
    if not _can_write(request, slug):
        messages.error(request, "صلاحيتك على هذا المستند للاطلاع فقط.")
    elif not files:
        messages.error(request, "اختر ملفاً لإرفاقه.")
    elif any(f.size > MAX_ATTACHMENT_BYTES for f in files):
        messages.error(request, "حجم الملف يتجاوز 10 ميجابايت.")
    else:
        description = request.POST.get("description", "").strip()[:255]
        for f in files:
            Attachment.objects.create(doctype=slug, object_id=obj.pk, file_name=f.name[:255],
                                      content_type=(f.content_type or "")[:100], size=f.size, content=f.read(),
                                      description=description, uploaded_by=request.user)
        messages.success(request, f"تم إرفاق {len(files)} ملف.")
    return redirect(obj.get_absolute_url())


@login_required
def download(request, slug, pk, attachment_id):
    dt, obj = _document(request, slug, pk)
    attachment = get_object_or_404(Attachment, pk=attachment_id, doctype=slug, object_id=obj.pk)
    inline = request.GET.get("inline") == "1" and attachment.content_type in ("application/pdf", "image/png",
                                                                                "image/jpeg")
    response = HttpResponse(bytes(attachment.content),
                            content_type=attachment.content_type if inline else "application/octet-stream")
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(attachment.file_name)}"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@login_required
@require_POST
def delete(request, slug, pk, attachment_id):
    dt, obj = _document(request, slug, pk)
    attachment = get_object_or_404(Attachment, pk=attachment_id, doctype=slug, object_id=obj.pk)
    membership = getattr(request, "membership", None)
    if not _can_write(request, slug) or (membership is not None and not membership.may("delete")):
        messages.error(request, "ليست لديك صلاحية حذف المرفقات.")
    else:
        attachment.delete()
        messages.success(request, "تم حذف المرفق.")
    return redirect(obj.get_absolute_url())
