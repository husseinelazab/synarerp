"""سجل أنواع المستندات (DocTypes) الذي تُبنى عليه الواجهات العامة."""
from dataclasses import dataclass, field
from typing import Optional

from django.http import Http404

MODULES = {
    "accounts": "المحاسبة",
    "selling": "المبيعات",
    "crm": "إدارة العملاء (CRM)",
    "projects": "المشاريع",
    "support": "الصيانة وخدمة العملاء",
    "buying": "المشتريات",
    "stock": "المخزون",
    "hr": "الموارد البشرية",
    "pos": "نقاط البيع",
    "funds": "الأموال المقيدة والتبرعات",
    "resources": "إدارة الموارد المالية",
    "members": "إدارة الأعضاء المشاركين",
    "awqaf": "إدارة الأوقاف",
    "grants": "إدارة المنح",
    "volunteering": "إدارة التطوع",
    "office": "المكتب الإلكتروني",
    "documents": "إدارة التوثيق والمستندات",
    "governance": "إدارة الحوكمة",
    "beneficiaries": "إدارة التقييم والمتابعة",
    "entities": "إدارة الجهات المستفيدة",
    "programs": "إدارة البرامج والتطوير",
    "excellence": "إدارة التميز المؤسسي",
    "relations": "إدارة العلاقات العامة والإعلام",
    "fleet": "إدارة الحركة والصيانة",
    "website": "إدارة الموقع الإلكتروني",
    "store": "المتجر الإلكتروني",
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
    # أقل عدد للأسطر في النموذج (صفر يسمح بحفظ المستند بلا أسطر لتُجلب تلقائياً)
    min_rows: int = 1
    # الأعمدة المعروضة في صفحة المستند والطباعة إن اختلفت عن حقول الإدخال
    display_fields: Optional[list] = None
    help: str = ""


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
    detail_template: str = ""  # قالب إضافي يُعرض في صفحة المستند
    detail_exclude: list = field(default_factory=list)
    list_actions: list = field(default_factory=list)
    # حقل اختيار يفصل المستند إلى نوعين بعناوين مستقلة (مثل سند قبض / سند صرف): {القيمة: (المفرد، الجمع)}.
    # إن حُدد النوع من الرابط يُخفى الحقل في النموذج وتصبح عناوين القائمة والنموذج باسم النوع.
    type_field: str = ""
    type_titles: dict = field(default_factory=dict)
    # حقول نادرة الاستخدام تُطوى في «خيارات إضافية» أسفل الحقول الأساسية حتى يبقى النموذج بسيطاً
    advanced_fields: list = field(default_factory=list)  # [(العنوان، اسم المسار)] أزرار إضافية (POST) في صفحة القائمة  # حقول يعرضها القالب الإضافي بدلاً من التفاصيل العامة

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


# الحقول الإضافية للمستندات التجارية (الفواتير والعروض والأوامر وأذون التسليم والاستلام)
COMMERCIAL_ADVANCED = ["price_list", "tax_rate", "currency", "project", "vat_category", "vat_exemption_reason",
                       "prices_include_tax", "exchange_rate", "payment_terms", "cost_center", "fund", "sales_person",
                       "coupon_code", "ignore_pricing_rules", "loyalty_points", "update_stock"]

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
