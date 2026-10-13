"""صلاحيات المستخدم العادي: الوحدات المسموحة، اعتماد المستندات، والفروع (مراكز التكلفة)."""
from django.core.exceptions import ValidationError

PERMISSION_MODULES = [
    ("selling", "المبيعات"),
    ("crm", "إدارة العملاء (CRM)"),
    ("projects", "المشاريع"),
    ("support", "الصيانة وخدمة العملاء"),
    ("buying", "المشتريات"),
    ("accounts", "المحاسبة والأصول الثابتة"),
    ("stock", "المخزون"),
    ("manufacturing", "التصنيع"),
    ("hr", "الموارد البشرية"),
    ("pos", "نقاط البيع"),
    ("einvoice", "الفوترة الإلكترونية"),
    ("funds", "الأموال المقيدة والتبرعات"),
    ("beneficiaries", "إدارة التقييم والمتابعة (المستفيدون)"),
    ("entities", "إدارة الجهات المستفيدة"),
]

# وحدات الجهات غير الربحية: لا تظهر للمنشآت التجارية ولا تُفتح لها
NONPROFIT_MODULES = {"funds", "beneficiaries", "entities"}
# الوحدات التجارية التي لا تظهر في قائمة الجهة غير الربحية
COMMERCIAL_MODULES = {"selling", "crm", "support", "manufacturing", "pos"}
# مسميات الوحدات للجهة غير الربحية (بمسميات «مكونات النظام» المعتادة في أنظمة الجمعيات)
NONPROFIT_LABELS = {
    "accounts": "إدارة الشؤون المالية",
    "buying": "المشتريات والموردون",
    "hr": "إدارة الموارد البشرية",
    "stock": "إدارة المخازن والمستودعات",
    "projects": "إدارة المشاريع",
    "settings": "إدارة التمكين التقني",
}


def company_modules(company):
    """الوحدات التي تخص نوع المنشأة لاختيار صلاحيات المستخدمين، بمسمياتها لدى هذا النوع."""
    if company is not None and company.is_nonprofit:
        return [(key, NONPROFIT_LABELS.get(key, label)) for key, label in PERMISSION_MODULES
                if key not in COMMERCIAL_MODULES]
    return [(key, label) for key, label in PERMISSION_MODULES if key not in NONPROFIT_MODULES]


def company_allows_path(company, path):
    """وحدات الجمعيات لا تُفتح للمنشأة التجارية."""
    modules = modules_for_path(path)
    return not modules or not modules <= NONPROFIT_MODULES or company.is_nonprofit

# الوحدات التي يمكن فتحها أو إغلاقها في الباقات (المحاسبة أساس النظام فلا تُغلق)
CORE_MODULES = {"accounts"}
PLAN_MODULES = [(key, label) for key, label in PERMISSION_MODULES if key not in CORE_MODULES]

# مسارات غير المستندات ← الوحدة
PATH_MODULES = [
    # أرصدة العملاء والموردين تظهر في قائمتي المبيعات والمشتريات
    ("/reports/accounts/balances/customer/", {"selling", "accounts"}),
    ("/reports/accounts/balances/supplier/", {"buying", "accounts"}),
    ("/reports/accounts/aging/customer/", {"selling", "accounts"}),
    ("/reports/accounts/aging/supplier/", {"buying", "accounts"}),
    ("/reports/accounts/reconcile/", {"selling", "buying", "accounts"}),
    ("/reports/accounts/cheques/", {"selling", "buying", "accounts"}),
    ("/reports/accounts/", "accounts"),
    ("/assets/", "accounts"),
    ("/reports/stock/gross-profit/", {"selling", "accounts", "stock", "manufacturing"}),
    ("/reports/stock/batches/", {"stock", "manufacturing"}),
    ("/reports/stock/reorder/", {"stock", "buying"}),
    ("/reports/stock/", "stock"),
    ("/stock/", "stock"),
    ("/messaging/", "selling"),
    ("/buying/", "buying"),
    ("/selling/", "selling"),
    ("/crm/", "crm"),
    ("/projects/", "projects"),
    ("/support/", "support"),
    ("/manufacturing/", "manufacturing"),
    ("/pos/", "pos"),
    ("/einvoice/", "einvoice"),
    ("/hr/", "hr"),
    ("/funds/", "funds"),
    ("/beneficiaries/entities/", "entities"),
    ("/beneficiaries/", "beneficiaries"),
]
# مستندات مشتركة بين أكثر من وحدة
SHARED_DOCTYPES = {"payment-entry": {"selling", "buying", "accounts"},
                   "payment-reconciliation": {"selling", "buying", "accounts"},
                   "price-list": {"selling", "buying", "stock"}, "item-price": {"selling", "buying", "stock"},
                   "pricing-rule": {"selling", "buying", "stock"},
                   "payment-terms": {"selling", "buying", "accounts"},
                   "cheque": {"selling", "buying", "accounts"},
                   "material-request": {"buying", "stock", "manufacturing"}}


def modules_for_path(path):
    """مجموعة الوحدات التي تكفي إحداها لفتح المسار، أو None إن كان المسار عاماً."""
    from core.registry import REGISTRY

    if path.startswith("/app/"):
        slug = path.split("/")[2]
        if slug in SHARED_DOCTYPES:
            return SHARED_DOCTYPES[slug]
        dt = REGISTRY.get(slug)
        if dt is None or dt.module in ("setup", "core"):
            return None
        return {dt.module}
    for prefix, module in PATH_MODULES:
        if path.startswith(prefix):
            return module if isinstance(module, set) else {module}
    return None


def plan_allows_path(plan, path):
    """هل تتيح باقة الشركة هذا المسار؟ (المسار المشترك يكفي أن تكون إحدى وحداته مفتوحة)"""
    modules = modules_for_path(path)
    return plan is None or modules is None or any(plan.allows_module(m) for m in modules)


# مسارات تكشف التكاليف والأرباح (تُحجب عمن أُخفيت عنه التكاليف)
COST_PATHS = ("/reports/stock/gross-profit/", "/reports/stock/pricing/", "/reports/stock/price-history/",
              "/reports/accounts/profit-and-loss/", "/reports/accounts/balance-sheet/")


def path_allowed(membership, path):
    if not company_allows_path(membership.company, path):
        return False
    if path.startswith("/app/"):
        parts = path.split("/")
        slug = parts[2]
        access = membership.doctype_access(slug)
        if access == "none":
            return False
        if access == "read" and len(parts) > 3 and (parts[3] in ("new", "import") or
                                                     (len(parts) > 4 and parts[4] not in ("", "print"))):
            return False  # الاطلاع فقط: لا إنشاء ولا تعديل ولا إجراءات
        if len(parts) > 3 and parts[3] == "export" and not membership.may("export"):
            return False
    if path.startswith(COST_PATHS) and not membership.may("costs"):
        return False
    modules = modules_for_path(path)
    return modules is None or any(membership.allows_module(m) for m in modules)


def require_action(request, action):
    membership = getattr(request, "membership", None)
    if membership is not None and not membership.may(action):
        labels = {"cancel": "إلغاء المستندات", "delete": "الحذف", "export": "التصدير"}
        raise ValidationError(f"ليست لديك صلاحية {labels.get(action, action)}.")


def require_submit(request):
    membership = getattr(request, "membership", None)
    if membership is not None and not membership.may_submit:
        raise ValidationError("صلاحيتك إنشاء المسودات فقط؛ يعتمدها أو يلغيها مستخدم آخر لديه صلاحية الاعتماد.")


def allowed_centers(request):
    membership = getattr(request, "membership", None)
    return membership.allowed_cost_center_ids() if membership is not None else None


def has_cost_center(model):
    return any(f.name == "cost_center" for f in model._meta.concrete_fields)


def scope_queryset(request, model, qs):
    """تقصر قائمة المستندات على فروع المستخدم إن كان مقيداً بفروع."""
    ids = allowed_centers(request)
    if ids is not None and has_cost_center(model):
        qs = qs.filter(cost_center_id__in=ids)
    return qs
