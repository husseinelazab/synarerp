"""سجل أنواع المستندات (DocTypes) الذي تُبنى عليه الواجهات العامة."""
from dataclasses import dataclass, field
from typing import Optional

from django.http import Http404

MODULES = {
    "accounts": "المحاسبة",
    "selling": "المبيعات",
    "buying": "المشتريات",
    "stock": "المخزون",
    "hr": "الموارد البشرية",
    "setup": "الإعدادات",
}


@dataclass
class ChildTable:
    model: type
    fields: list
    label: str = "البنود"
    fk_name: str = "parent"
    # نوع السعر الذي يُجلب تلقائياً عند اختيار الصنف: selling أو buying
    rate_type: str = ""


@dataclass
class DocType:
    slug: str
    model: type
    module: str
    list_fields: list
    form_class: Optional[type] = None
    form_fields: Optional[list] = None
    search_fields: list = field(default_factory=list)
    child: Optional[ChildTable] = None
    totals: list = field(default_factory=list)  # حقول الإجماليات المعروضة في صفحة المستند
    show_in_menu: bool = True

    @property
    def label(self):
        return self.model._meta.verbose_name

    @property
    def label_plural(self):
        return self.model._meta.verbose_name_plural

    @property
    def is_submittable(self):
        from core.models import SubmittableDocument

        return issubclass(self.model, SubmittableDocument)


REGISTRY: dict[str, DocType] = {}


def register(doctype: DocType):
    REGISTRY[doctype.slug] = doctype
    doctype.model.doctype_slug = doctype.slug
    return doctype


def get_doctype(slug) -> DocType:
    try:
        return REGISTRY[slug]
    except KeyError:
        raise Http404("نوع المستند غير موجود")


def menu():
    """قائمة الوحدات وما تحتويه من مستندات لعرضها في الشريط الجانبي."""
    result = []
    for key, label in MODULES.items():
        items = [dt for dt in REGISTRY.values() if dt.module == key and dt.show_in_menu]
        if items:
            result.append({"key": key, "label": label, "doctypes": items})
    return result


def voucher_label(slug):
    dt = REGISTRY.get(slug)
    return dt.label if dt else slug
