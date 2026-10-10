from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.forms import style_form
from core.importer import build_csv, build_workbook, columns_for, export_rows, import_key, run_import
from core.models import DataImport, ImportStatus
from core.registry import get_doctype

MAX_UPLOAD = 10 * 1024 * 1024


class ImportForm(forms.Form):
    file = forms.FileField(label="ملف Excel أو CSV")
    mode = forms.ChoiceField(label="طريقة الاستيراد", choices=[("insert", "إضافة سجلات جديدة"),
                                                                ("update", "إضافة وتحديث الموجود")])
    submit_after = forms.BooleanField(label="اعتماد المستندات وترحيل قيودها بعد الاستيراد", required=False)
    valid_only = forms.BooleanField(label="استيراد الصفوف الصحيحة فقط وتجاهل الأخطاء", required=False)

    def __init__(self, dt, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if dt.is_submittable or not import_key(dt.model):
            del self.fields["mode"]
        if not dt.is_submittable:
            del self.fields["submit_after"]
        style_form(self)

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if not upload.name.lower().endswith((".xlsx", ".csv")):
            raise forms.ValidationError("الصيغ المدعومة: xlsx و csv.")
        if upload.size > MAX_UPLOAD:
            raise forms.ValidationError("حجم الملف أكبر من 10 ميجابايت.")
        return upload


@login_required
def import_page(request, slug):
    dt = get_doctype(slug)
    form = ImportForm(dt, request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        job = DataImport(
            doctype_slug=slug, file_name=upload.name, content=upload.read(), created_by=request.user,
            mode=form.cleaned_data.get("mode", "insert"), submit_after=form.cleaned_data.get("submit_after", False),
            valid_only=form.cleaned_data.get("valid_only", False),
        )
        job.result = run_import(dt, job.file_name, bytes(job.content), job.mode, job.submit_after, job.valid_only,
                                dry_run=True, user=request.user)
        job.save()
        return redirect("doc_import_job", slug=slug, pk=job.pk)
    return render(request, "core/import.html", {
        "dt": dt, "form": form, "columns": columns_for(dt), "key": import_key(dt.model),
        "history": DataImport.objects.filter(doctype_slug=slug)[:10],
    })


@login_required
def import_job(request, slug, pk):
    dt = get_doctype(slug)
    job = get_object_or_404(DataImport, pk=pk, doctype_slug=slug)
    if request.method == "POST" and job.status == ImportStatus.PREVIEWED:
        job.result = run_import(dt, job.file_name, bytes(job.content), job.mode, job.submit_after, job.valid_only,
                                dry_run=False, user=request.user)
        imported = job.result["created"] + job.result["updated"]
        job.status = ImportStatus.DONE if imported else ImportStatus.FAILED
        job.result["executed_at"] = timezone.now().isoformat()
        job.save()
        if imported:
            messages.success(request, f"تم استيراد {imported} سجل بنجاح.")
        else:
            messages.error(request, "لم يُستورد أي سجل. راجع الأخطاء وصحح الملف ثم أعد رفعه.")
        return redirect("doc_import_job", slug=slug, pk=pk)
    result = job.result
    can_execute = job.status == ImportStatus.PREVIEWED and not result.get("missing_columns") and (
        (result.get("failed", 0) == 0 and result.get("total", 0) > 0)
        or (job.valid_only and result.get("total", 0) > result.get("failed", 0)))
    return render(request, "core/import_job.html", {"dt": dt, "job": job, "result": result, "can_execute": can_execute})


def _download(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
    return response


@login_required
def import_template(request, slug, fmt):
    from urllib.parse import quote

    dt = get_doctype(slug)
    columns = columns_for(dt)
    name = quote(f"قالب-{dt.label_plural}")
    if fmt == "csv":
        return _download(build_csv(columns, []), f"{name}.csv", "text/csv; charset=utf-8")
    return _download(build_workbook(dt, columns, []), f"{name}.xlsx",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@login_required
def export_list(request, slug):
    from urllib.parse import quote

    from core.views import filtered_queryset

    dt = get_doctype(slug)
    columns, rows = export_rows(dt, filtered_queryset(request, dt)[0][:20000])
    name = quote(f"{dt.label_plural}-{timezone.localdate():%Y-%m-%d}")
    return _download(build_workbook(dt, columns, rows, with_help=False), f"{name}.xlsx",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
