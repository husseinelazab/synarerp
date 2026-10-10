"""القائمة الجانبية الهرمية: وحدات رئيسية تحتها مستنداتها وتقاريرها."""
from django.urls import reverse

# أيقونات خطية بسيطة (SVG) لكل وحدة
ICONS = {
    "home": '<path d="M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "selling": '<path d="M3 3h2l2.4 12.2a2 2 0 0 0 2 1.8h7.7a2 2 0 0 0 2-1.6L21 8H6"/><circle cx="10" cy="20" r="1.5"/><circle cx="17" cy="20" r="1.5"/>',
    "buying": '<path d="M6 2l-2 4v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V6l-2-4z"/><path d="M4 6h16M16 10a4 4 0 0 1-8 0"/>',
    "accounts": '<path d="M4 4h16v16H4z"/><path d="M4 9h16M9 9v11M14 13h3M14 16h3"/>',
    "stock": '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8M12 13v8"/>',
    "pos": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M7 20h10M12 16v4M7 8h4"/>',
    "hr": '<circle cx="9" cy="8" r="3.5"/><path d="M2 21a7 7 0 0 1 14 0M16 3.5a3.5 3.5 0 0 1 0 7M22 21a7 7 0 0 0-4-6.3"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "platform": '<path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z"/>',
    "einvoice": '<path d="M6 2h9l5 5v15H6z"/><path d="M15 2v5h5M9 13h2v2H9zM13 13h2v2h-2zM9 17h2v2H9z"/>',
}


def _item(label, name, *args, query=""):
    return {"label": label, "url": reverse(name, args=args) + query}


def build_menu(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        if request.user.is_superuser:
            return [{"key": "platform", "label": "إدارة المنصة", "icon": ICONS["platform"],
                     "sections": [{"items": [_item("الشركات والاشتراكات", "platform_dashboard"),
                                             _item("خادم البريد", "platform_email_settings")]}]}]
        return []
    subscription = getattr(request.company, "subscription", None)
    doc = lambda label, slug: _item(label, "doc_list", slug)  # noqa: E731
    if membership.role == "employee":
        return _mark_active(request, [{"key": "hr", "label": "بوابة الموظف", "sections": [{"items": [
            _item("صفحتي", "portal_home"), _item("طلب إجازة", "portal_leave"), _item("طلب سلفة", "portal_loan"),
            _item("حضوري", "portal_attendance"), _item("حسابي", "profile")]}]}])

    modules = [
        {"key": "selling", "label": "المبيعات", "sections": [
            {"items": [doc("عروض الأسعار", "quotation"), doc("أوامر البيع", "sales-order"),
                       doc("فواتير المبيعات", "sales-invoice"), doc("العملاء", "customer"),
                       _item("سندات القبض", "doc_list", "payment-entry", query="?payment_type=receive")]},
            {"title": "تقارير", "items": [_item("أرصدة العملاء", "party_balances", "customer")]},
        ]},
        {"key": "buying", "label": "المشتريات", "sections": [
            {"items": [doc("أوامر الشراء", "purchase-order"), doc("فواتير المشتريات", "purchase-invoice"),
                       doc("الموردون", "supplier"),
                       _item("سندات الصرف", "doc_list", "payment-entry", query="?payment_type=pay")]},
            {"title": "تقارير", "items": [_item("أرصدة الموردين", "party_balances", "supplier")]},
        ]},
        {"key": "accounts", "label": "المحاسبة", "sections": [
            {"items": [doc("قيود اليومية", "journal-entry"), doc("سندات القبض والصرف", "payment-entry"),
                       _item("دليل الحسابات", "chart_of_accounts"), doc("مراكز التكلفة والفروع", "cost-center")]},
            {"title": "البنوك", "items": [_item("التسوية البنكية", "bank_reconciliation"),
                                         doc("حركات كشف البنك", "bank-transaction")]},
            {"title": "تقارير", "items": [
                _item("دفتر اليومية العام", "general_journal"), _item("دفتر الأستاذ العام", "general_ledger"),
                _item("ميزان المراجعة", "trial_balance"), _item("قائمة الدخل", "profit_and_loss"),
                _item("الميزانية العمومية", "balance_sheet"), _item("إقرار ضريبة القيمة المضافة", "vat_report"),
            ]},
            {"title": "بداية العمل", "items": [
                _item("معالج الأرصدة الافتتاحية", "opening_wizard"), doc("أرصدة الحسابات", "opening-account"),
                doc("أرصدة العملاء والموردين", "opening-party"), doc("المخزون الافتتاحي", "opening-stock"),
            ]},
            {"title": "نهاية السنة", "items": [doc("إقفال السنة المالية", "period-closing")]},
        ]},
        {"key": "stock", "label": "المخزون", "sections": [
            {"items": [doc("الأصناف", "item"), doc("الحركات المخزنية", "stock-entry"), doc("المستودعات", "warehouse"),
                       doc("مجموعات الأصناف", "item-group"), doc("وحدات القياس", "uom")]},
            {"title": "تقارير", "items": [_item("أرصدة المخزون", "stock_balance"), _item("دفتر حركة المخزون", "stock_ledger")]},
        ]},
    ]
    einvoice_items = [_item("الفواتير الإلكترونية", "einvoice_list")]
    if membership.can_manage:
        einvoice_items.append(_item("الربط مع منصة فاتورة", "einvoice_settings"))
    modules.append({"key": "einvoice", "label": "الفوترة الإلكترونية", "sections": [{"items": einvoice_items}]})
    if subscription and subscription.plan.includes_pos:
        modules.append({"key": "pos", "label": "نقاط البيع", "sections": [
            {"items": [_item("شاشة نقطة البيع", "pos_terminal"), _item("الورديات", "pos_shifts"),
                       doc("ملفات نقاط البيع", "pos-profile")]},
        ]})
    modules.append({"key": "hr", "label": "الموارد البشرية", "sections": [
        {"items": [_item("لوحة الموارد البشرية", "hr_dashboard"), doc("الموظفون", "employee"),
                   doc("مسيرات الرواتب", "payroll-run"),
                   doc("سلف الموظفين", "employee-loan"), doc("مخالصات نهاية الخدمة", "eos-settlement")]},
        {"title": "الحضور والانصراف", "items": [_item("تسجيل الحضور اليومي", "attendance_daily"),
                                                 _item("تقرير الحضور الشهري", "attendance_monthly"),
                                                 doc("سجل الحضور", "attendance"),
                                                 _item("استيراد سجلات البصمة", "attendance_punches")]},
        {"title": "الإجازات", "items": [doc("طلبات الإجازات", "leave-application"), _item("أرصدة الإجازات", "leave_balances"),
                                        doc("أنواع الإجازات", "leave-type")]},
        {"title": "الأداء", "items": [doc("تقييمات الأداء", "appraisal"), doc("نماذج التقييم", "appraisal-template")]},
        {"title": "تقارير", "items": [_item("مخصص نهاية الخدمة", "eos_provision")]},
        {"title": "الإعداد", "items": ([_item("إعدادات الرواتب والتأمينات", "payroll_settings")] if membership.can_manage else [])
         + [doc("ورديات العمل", "work-shift"), doc("جدول الورديات", "shift-assignment"), doc("العطل الرسمية", "holiday"),
            doc("الأقسام", "department"), doc("المسميات الوظيفية", "designation")]},
    ]})
    settings_items = [doc("السنوات المالية", "fiscal-year"), doc("أسعار الصرف", "exchange-rate")]
    if membership.can_manage:
        settings_items = [_item("إعدادات الشركة", "company_settings"), _item("المستخدمون والصلاحيات", "users_list"),
                          _item("البريد الإلكتروني", "company_email_settings"),
                          _item("الاشتراك والفوترة", "billing")] + settings_items
    modules.append({"key": "settings", "label": "الإعدادات", "sections": [{"items": settings_items}]})
    if request.user.is_superuser:
        modules.append({"key": "platform", "label": "إدارة المنصة", "sections": [
            {"items": [_item("الشركات والاشتراكات", "platform_dashboard"), _item("خادم البريد", "platform_email_settings")]}]})

    return _mark_active(request, modules)


def _mark_active(request, modules):
    # تحديد العنصر النشط (الأطول تطابقاً) وفتح وحدته
    path = request.get_full_path()
    best, best_len = None, 0
    for module in modules:
        module["icon"] = ICONS[module["key"]]
        for section in module["sections"]:
            for item in section["items"]:
                base = item["url"].split("?")[0]
                exact = item["url"] == path
                if (exact or path.startswith(base)) and (len(item["url"]) if exact else len(base)) > best_len:
                    best, best_len = (module, item), (len(item["url"]) + 1000 if exact else len(base))
    if best:
        best[0]["open"] = True
        best[1]["active"] = True
    return modules
