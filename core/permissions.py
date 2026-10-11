"""صلاحيات المستخدم العادي: الوحدات المسموحة، اعتماد المستندات، والفروع (مراكز التكلفة)."""
from django.core.exceptions import ValidationError

PERMISSION_MODULES = [
    ("selling", "المبيعات"),
    ("buying", "المشتريات"),
    ("accounts", "المحاسبة والأصول الثابتة"),
    ("stock", "المخزون"),
    ("hr", "الموارد البشرية"),
    ("pos", "نقاط البيع"),
    ("einvoice", "الفوترة الإلكترونية"),
]

# مسارات غير المستندات ← الوحدة
PATH_MODULES = [
    # أرصدة العملاء والموردين تظهر في قائمتي المبيعات والمشتريات
    ("/reports/accounts/balances/customer/", {"selling", "accounts"}),
    ("/reports/accounts/balances/supplier/", {"buying", "accounts"}),
    ("/reports/accounts/", "accounts"),
    ("/assets/", "accounts"),
    ("/reports/stock/gross-profit/", {"selling", "accounts", "stock"}),
    ("/reports/stock/", "stock"),
    ("/stock/", "stock"),
    ("/pos/", "pos"),
    ("/einvoice/", "einvoice"),
    ("/hr/", "hr"),
]
# مستندات مشتركة بين أكثر من وحدة
SHARED_DOCTYPES = {"payment-entry": {"selling", "buying", "accounts"}}


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


def path_allowed(membership, path):
    modules = modules_for_path(path)
    return modules is None or any(membership.allows_module(m) for m in modules)


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
