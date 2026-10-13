"""البيانات الأساسية للمكتب الإلكتروني والتوثيق عند تأسيس الجهة غير الربحية."""

REQUEST_TYPES = [("طلب شراء", "مواد أو خدمات للجمعية"), ("طلب صيانة", "مبنى أو جهاز أو سيارة"),
                 ("طلب عهدة", "عهدة نقدية أو عينية"), ("طلب خطاب تعريف", "خطاب تعريف بالموظف"),
                 ("طلب استخدام سيارة", "مشوار عمل"), ("طلب آخر", "")]
ARCHIVE_CATEGORIES = ["التراخيص والصكوك", "العقود والاتفاقيات", "المحاضر", "الخطابات", "المالية", "الموارد البشرية"]


def ensure_office_setup(company):
    from core.models import ApprovalRule
    from office.models import AdminRequestType, ArchiveCategory

    if AdminRequestType.all_objects.filter(company=company).exists():
        return
    AdminRequestType.all_objects.bulk_create([AdminRequestType(company=company, name=n, description=d)
                                              for n, d in REQUEST_TYPES])
    ArchiveCategory.all_objects.bulk_create([ArchiveCategory(company=company, name=n) for n in ARCHIVE_CATEGORIES])
    # الطلب الإداري يعتمده المدير (يمكن إضافة مستويات من «سلاسل الاعتماد»)
    ApprovalRule.all_objects.create(company=company, doctype="admin-request", level=1, approver_role="manager")
