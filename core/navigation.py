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
    "crm": '<path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0"/><path d="M18 4l2 2-4 4"/>',
    "projects": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h6M7 13h10M7 17h4"/>',
    "support": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
    "beneficiaries": '<path d="M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 20a6 6 0 0 1 12 0"/><path d="M16 11l2 2 4-4"/>',
    "resources": '<path d="M12 21s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 5.5-7 10-7 10z"/>',
    "members": '<circle cx="8" cy="8" r="3"/><circle cx="16" cy="8" r="3"/><path d="M2 20a6 6 0 0 1 12 0M10 20a6 6 0 0 1 12 0"/>',
    "governance": '<path d="M12 3l9 4H3zM5 10v7M10 10v7M14 10v7M19 10v7M3 21h18"/>',
    "awqaf": '<path d="M4 21V10l8-6 8 6v11M9 21v-5a3 3 0 0 1 6 0v5M2 21h20"/>',
    "grants": '<path d="M3 11l4-4 4 3 4-4 6 5"/><path d="M3 20h18M6 20v-5M11 20v-7M16 20v-4M20 20v-6"/>',
    "volunteering": '<path d="M7 11V7a2 2 0 0 1 4 0v4M11 9a2 2 0 0 1 4 0v2M15 10a2 2 0 0 1 4 0v4a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-3l-2-4a2 2 0 0 1 3-2l2 2"/>',
    "office": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18"/>',
    "documents": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    "entities": '<path d="M3 21h18M5 21V8l7-5 7 5v13M9 21v-6h6v6"/>',
    "platform": '<path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z"/>',
    "manufacturing": '<path d="M3 21V10l5 3V10l5 3V7l8 4v10z"/><path d="M7 17h2M12 17h2M17 17h2"/>',
    "quran": '<path d="M3 4h7a2 2 0 0 1 2 2v15a4 4 0 0 0-4-2H3zM21 4h-7a2 2 0 0 0-2 2v15a4 4 0 0 1 4-2h5z"/>',
    "dawah": '<path d="M4 21V9l8-6 8 6v12M9 21v-7h6v7M2 21h20"/>',
    "health": '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z"/>',
    "programs": '<path d="M2 9l10-5 10 5-10 5z"/><path d="M6 11v5c0 1.5 3 3 6 3s6-1.5 6-3v-5M22 9v6"/>',
    "excellence": '<circle cx="12" cy="9" r="6"/><path d="M8.5 14l-1.5 8 5-3 5 3-1.5-8"/>',
    "relations": '<path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1zM15 9a3 3 0 0 1 0 6M18 6a7 7 0 0 1 0 12"/>',
    "fleet": '<path d="M3 13l2-5a2 2 0 0 1 2-1h10a2 2 0 0 1 2 1l2 5v4h-2M5 17H3v-4h18"/><circle cx="7.5" cy="17" r="1.5"/><circle cx="16.5" cy="17" r="1.5"/>',
    "website": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "store": '<path d="M4 4h2l2 11h10l2-8H7"/><circle cx="9" cy="19" r="1.5"/><circle cx="17" cy="19" r="1.5"/>',
    "statistics": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "einvoice": '<path d="M6 2h9l5 5v15H6z"/><path d="M15 2v5h5M9 13h2v2H9zM13 13h2v2h-2zM9 17h2v2H9z"/>',
}


def _item(label, name, *args, query=""):
    return {"label": label, "url": reverse(name, args=args) + query}


def platform_items():
    return [_item("الشركات والاشتراكات", "platform_dashboard"), _item("الباقات والوحدات", "platform_plans"),
            _item("خادم البريد", "platform_email_settings")]


def build_menu(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        if request.user.is_superuser:
            return _mark_active(request, [{"key": "platform", "label": "إدارة المنصة",
                                           "sections": [{"items": platform_items()}]}])
        return []
    subscription = getattr(request.company, "subscription", None)
    includes_ai = bool(subscription and subscription.plan.includes_ai)
    doc = lambda label, slug: _item(label, "doc_list", slug)  # noqa: E731
    if membership.role == "employee":
        return _mark_active(request, [{"key": "hr", "label": "بوابة الموظف", "sections": [{"items": [
            _item("صفحتي", "portal_home"), _item("طلب إجازة", "portal_leave"), _item("طلب سلفة", "portal_loan"),
            _item("حضوري", "portal_attendance"), _item("حسابي", "profile")]}]}])

    # كل وحدة: أقسام بعناوين ثابتة (العمليات، الأطراف والمدفوعات، الإعداد، التقارير)، وكل عنصر في مكان واحد قدر الإمكان
    modules = [
        {"key": "selling", "label": "المبيعات", "sections": [
            {"title": "المبيعات", "items": [doc("عروض الأسعار", "quotation"), doc("أوامر البيع", "sales-order"),
                                           doc("أذون التسليم", "delivery-note"), doc("فواتير المبيعات", "sales-invoice")]},
            {"title": "العملاء والتحصيل", "items": [
                doc("العملاء", "customer"),
                _item("سندات القبض", "doc_list", "payment-entry", query="?payment_type=receive"),
                _item("شيكات العملاء", "doc_list", "cheque", query="?direction=received"),
                _item("تسوية دفعات العملاء", "payment_reconcile", query="?party_type=customer")]},
            {"title": "التسعير والعروض", "items": [
                doc("قوائم الأسعار", "price-list"), doc("أسعار الأصناف في القوائم", "item-price"),
                doc("العروض وقواعد التسعير", "pricing-rule"), doc("برامج الولاء", "loyalty-program"),
                doc("حركات نقاط الولاء", "loyalty-point-entry"),
                _item("تحليل التسعير والربحية", "pricing_analysis"), _item("سجل تغيّر الأسعار", "price_history")]},
            {"title": "فريق المبيعات", "items": [doc("مندوبو المبيعات", "sales-person"), doc("مناطق البيع", "territory"),
                                                doc("فئات العملاء", "customer-group")]},
            {"title": "التقارير", "items": [_item("أرصدة العملاء", "party_balances", "customer"),
                                           _item("أعمار ديون العملاء", "aging_report", "customer"),
                                           _item("المبيعات والعمولات", "sales_by_person"),
                                           _item("الربحية (مجمل الربح)", "gross_profit")]},
        ]},
        {"key": "buying", "label": "المشتريات", "sections": [
            {"title": "المشتريات", "items": [
                doc("طلبات المواد", "material-request"), doc("عروض أسعار الموردين", "supplier-quotation"),
                doc("أوامر الشراء", "purchase-order"), doc("أذون الاستلام", "purchase-receipt"),
                doc("فواتير المشتريات", "purchase-invoice"), doc("تكاليف الشحن والجمارك", "landed-cost")]
                + ([_item("قراءة فاتورة مورد بالذكاء الاصطناعي", "ai_invoice_upload")] if includes_ai else [])},
            {"title": "الموردون والسداد", "items": [
                doc("الموردون", "supplier"),
                _item("سندات الصرف", "doc_list", "payment-entry", query="?payment_type=pay"),
                _item("شيكات الموردين", "doc_list", "cheque", query="?direction=issued"),
                _item("تسوية دفعات الموردين", "payment_reconcile", query="?party_type=supplier")]},
            {"title": "التقارير", "items": [_item("أرصدة الموردين", "party_balances", "supplier"),
                                           _item("أعمار ديون الموردين", "aging_report", "supplier")]},
        ]},
        {"key": "stock", "label": "المخزون", "sections": [
            {"title": "الحركات", "items": [doc("الحركات المخزنية", "stock-entry"), doc("الجرد", "stock-count"),
                                          doc("التشغيلات", "batch"), doc("الأرقام التسلسلية والضمان", "serial-no")]},
            {"title": "الأصناف والمستودعات", "items": [doc("الأصناف", "item"), doc("مجموعات الأصناف", "item-group"),
                                                      doc("وحدات القياس", "uom"), doc("المستودعات", "warehouse")]},
            {"title": "الجودة", "items": [doc("فحوصات الجودة", "quality-inspection"), doc("نماذج الفحص", "quality-template")]},
            {"title": "الباركود والموازين", "items": [_item("طباعة الملصقات", "stock_labels"),
                                                     _item("فاحص الأسعار", "stock_price_checker"),
                                                     _item("الموازين", "stock_scales")]},
            {"title": "التقارير", "items": [_item("أرصدة المخزون", "stock_balance"), _item("دفتر حركة المخزون", "stock_ledger"),
                                           _item("التشغيلات والصلاحية", "batch_report"),
                                           _item("حد إعادة الطلب", "reorder_report")]},
        ]},
        {"key": "accounts", "label": "المحاسبة", "sections": [
            {"title": "القيود والسندات", "items": [
                doc("قيود اليومية", "journal-entry"),
                _item("سندات القبض", "doc_list", "payment-entry", query="?payment_type=receive"),
                _item("سندات الصرف", "doc_list", "payment-entry", query="?payment_type=pay"),
                doc("المستندات المتكررة", "recurring-document"),
                doc("المصروفات المقدمة والإيرادات المؤجلة", "deferral")]},
            {"title": "الحسابات والموازنات", "items": [
                _item("دليل الحسابات", "chart_of_accounts"), doc("مراكز التكلفة والفروع", "cost-center"),
                doc("الموازنات التقديرية", "budget"), _item("الموازنة مقابل الفعلي", "budget_report")]},
            {"title": "البنوك والشيكات", "items": [
                _item("التسوية البنكية", "bank_reconciliation"), doc("حركات كشف البنك", "bank-transaction"),
                _item("الشيكات المستحقة", "cheques_due"), doc("حافظة الشيكات", "cheque")]},
            {"title": "الذمم والعهد", "items": [
                _item("تسوية الدفعات", "payment_reconcile"), doc("مستندات تسوية الدفعات", "payment-reconciliation"),
                doc("شروط الدفع والأقساط", "payment-terms"),
                doc("العهد النقدية", "cash-custody"), doc("تسويات العهد", "custody-settlement")]},
            {"title": "الأصول الثابتة", "items": [doc("الأصول", "asset"), _item("ترحيل الإهلاك", "asset_depreciation"),
                                                 _item("سجل الأصول", "asset_register"), doc("فئات الأصول", "asset-category")]},
            {"title": "القوائم المالية", "items": [
                _item("دفتر اليومية العام", "general_journal"), _item("دفتر الأستاذ العام", "general_ledger"),
                _item("ميزان المراجعة", "trial_balance"), _item("قائمة الدخل", "profit_and_loss"),
                _item("الميزانية العمومية", "balance_sheet"), _item("قائمة التدفقات النقدية", "cash_flow"),
                _item("قائمة الدخل المقارنة", "comparative_pnl"), _item("الميزانية المقارنة", "comparative_balance_sheet"),
                _item("القوائم الموحدة للشركات", "consolidated")]},
            {"title": "الضرائب والزكاة", "items": [
                _item("إقرار ضريبة القيمة المضافة", "vat_report"), _item("إقرار ضريبة الاستقطاع", "withholding_report"),
                doc("الإقرارات الزكوية", "zakat")]},
            {"title": "بداية العمل ونهاية الفترة", "items": [
                _item("معالج الأرصدة الافتتاحية", "opening_wizard"), doc("أرصدة الحسابات", "opening-account"),
                doc("أرصدة العملاء والموردين", "opening-party"), doc("المخزون الافتتاحي", "opening-stock"),
                doc("إعادة تقييم العملات الأجنبية", "exchange-revaluation"), doc("إقفال السنة المالية", "period-closing")]},
        ]},
    ]
    if subscription and subscription.plan.includes_pos:
        modules.append({"key": "pos", "label": "نقاط البيع", "sections": [
            {"items": [_item("شاشة نقطة البيع", "pos_terminal"), _item("الورديات", "pos_shifts"),
                       doc("ملفات نقاط البيع", "pos-profile")]},
        ]})
    einvoice_items = [_item("الفواتير الإلكترونية", "einvoice_list")]
    if membership.can_manage:
        einvoice_items.append(_item("الربط مع منصة فاتورة", "einvoice_settings"))
    modules += [
        {"key": "einvoice", "label": "الفوترة الإلكترونية", "sections": [{"items": einvoice_items}]},
        {"key": "manufacturing", "label": "التصنيع", "sections": [
            {"title": "الإنتاج", "items": [doc("أوامر الإنتاج", "production"), doc("قوائم المواد", "bom"),
                                          _item("تخطيط احتياجات المواد", "mrp"),
                                          doc("إعادة التعبئة والتفكيك", "repack"), doc("قوالب إعادة التعبئة", "repack-template")]},
            {"title": "الإعداد", "items": [doc("محطات العمل", "workstation"), doc("مسارات التشغيل", "routing")]},
            {"title": "التقارير", "items": [_item("الإنتاج والهالك", "production_report")]},
        ]},
        {"key": "crm", "label": "إدارة العملاء (CRM)", "sections": [
            {"items": [_item("مسار المبيعات", "crm_pipeline"), _item("متابعاتي", "crm_followups"),
                       doc("العملاء المحتملون", "lead"), doc("فرص البيع", "opportunity"), doc("المتابعات", "activity")]},
        ]},
        {"key": "projects", "label": "المشاريع", "sections": [
            {"items": [doc("المشاريع", "project"), doc("مهام المشاريع", "project-task"),
                       _item("ربحية المشاريع", "project_report")]},
        ]},
        {"key": "support", "label": "الصيانة وخدمة العملاء", "sections": [
            {"items": [_item("لوحة الصيانة", "support_dashboard"), doc("التذاكر", "service-ticket"),
                       doc("عقود الصيانة", "maintenance-contract"), doc("الزيارات", "maintenance-visit")]},
        ]},
    ]
    modules.append({"key": "hr", "label": "الموارد البشرية", "sections": [
        {"title": "الموظفون والرواتب", "items": [
            _item("لوحة الموارد البشرية", "hr_dashboard"), doc("الموظفون", "employee"), doc("مسيرات الرواتب", "payroll-run"),
            doc("سلف الموظفين", "employee-loan"), doc("مخالصات نهاية الخدمة", "eos-settlement")]},
        {"title": "الحضور والانصراف", "items": [_item("تسجيل الحضور اليومي", "attendance_daily"),
                                                 _item("تقرير الحضور الشهري", "attendance_monthly"),
                                                 doc("سجل الحضور", "attendance"),
                                                 _item("استيراد سجلات البصمة", "attendance_punches"),
                                                 _item("سجل البصمات الخام", "raw_punches")]
                                                + ([_item("أجهزة البصمة", "attendance_devices")] if membership.can_manage else [])},
        {"title": "الإجازات", "items": [doc("طلبات الإجازات", "leave-application"), _item("أرصدة الإجازات", "leave_balances"),
                                        doc("أنواع الإجازات", "leave-type")]},
        {"title": "الأداء", "items": [doc("تقييمات الأداء", "appraisal"), doc("نماذج التقييم", "appraisal-template")]},
        {"title": "التقارير", "items": [_item("مخصص نهاية الخدمة", "eos_provision")]},
        {"title": "الإعداد", "items": ([_item("إعدادات الرواتب والتأمينات", "payroll_settings")] if membership.can_manage else [])
         + [doc("ورديات العمل", "work-shift"), doc("جدول الورديات", "shift-assignment"), doc("العطل الرسمية", "holiday"),
            doc("الأقسام", "department"), doc("المسميات الوظيفية", "designation")]},
    ]})
    if membership.can_manage:
        settings_sections = [
            {"title": "الشركة", "items": [_item("إعدادات الشركة", "company_settings"), doc("السنوات المالية", "fiscal-year"),
                                         doc("أسعار الصرف", "exchange-rate"), _item("الطباعة والشعار", "print_settings")]},
            {"title": "المستخدمون والأمان", "items": [_item("المستخدمون والصلاحيات", "users_list"),
                                                     _item("سلاسل الاعتماد", "doc_list", "approval-rule"),
                                                     _item("سجل التعديلات", "audit_log")]},
            {"title": "التواصل", "items": [_item("البريد الإلكتروني", "company_email_settings"),
                                          _item("رسائل SMS وواتساب", "messaging_settings"), _item("سجل الرسائل", "message_log")]},
            {"title": "التكامل", "items": [_item("الواجهة البرمجية (API)", "api_keys")]
             + ([_item("الذكاء الاصطناعي", "ai_settings")] if includes_ai else [])},
            {"title": "الاشتراك والبيانات", "items": [_item("الاشتراك والفوترة", "billing"), _item("مسميات الوحدات", "module_labels"),
                                                     _item("تصدير كامل البيانات", "company_export")]},
        ]
    else:
        settings_sections = [{"items": [doc("السنوات المالية", "fiscal-year"), doc("أسعار الصرف", "exchange-rate")]}]
    modules.append({"key": "settings", "label": "الإعدادات", "sections": settings_sections})
    if request.company.is_nonprofit:
        modules = nonprofit_modules(modules, doc, request.company)
    if request.user.is_superuser:
        modules.append({"key": "platform", "label": "إدارة المنصة", "sections": [{"items": platform_items()}]})
    for module in modules:
        module["label"] = request.company.module_labels.get(module["key"], module["label"])
    modules = _grouped(modules, NONPROFIT_GROUPS if request.company.is_nonprofit else BUSINESS_GROUPS)

    # ما أغلقته باقة الشركة لا يظهر لأحد، ولا مالك الحساب
    plan = subscription.plan if subscription else None
    if plan is not None:
        from core.permissions import plan_allows_path

        modules = [m for m in modules if m["key"] in ("settings", "platform") or any(map(plan.allows_module, _keys(m)))]
        for module in modules:
            for section in module["sections"]:
                section["items"] = [i for i in section["items"] if plan_allows_path(plan, i["url"].split("?")[0])]
            module["sections"] = [s for s in module["sections"] if s["items"]]
        modules = [m for m in modules if m["sections"]]

    # الوحدات التي اختارت المنشأة إخفاءها لأنها لا تستخدمها (من إعدادات الشركة)
    hidden = set(request.company.hidden_modules or ())
    if hidden:
        modules = [m for m in modules if m["key"] not in hidden]

    # المستخدم العادي يرى الوحدات المسموحة له فقط، وداخلها ما يحق له فتحه
    modules = [m for m in modules if m["key"] in ("settings", "platform") or any(map(membership.allows_module, _keys(m)))]
    if not membership.can_manage:
        from core.permissions import path_allowed

        for module in modules:
            for section in module["sections"]:
                section["items"] = [i for i in section["items"] if path_allowed(membership, i["url"].split("?")[0])]
            module["sections"] = [s for s in module["sections"] if s["items"]]
        modules = [m for m in modules if m["sections"]]
    return _mark_active(request, modules)


# مجموعات القائمة الجانبية: عنوان صغير فوق كل مجموعة وحدات متقاربة، بالترتيب المعروض
NONPROFIT_GROUPS = [
    ("العمل اليومي", ["office", "documents"]),
    ("الخدمات والبرامج", ["beneficiaries", "entities", "programs", "quran", "dawah", "health", "volunteering", "projects"]),
    ("الموارد والمالية", ["resources", "grants", "accounts", "awqaf", "stock"]),
    ("الحوكمة والتميز", ["members", "governance", "excellence", "relations"]),
    ("القنوات الرقمية", ["store", "website"]),
    ("الخدمات المساندة", ["hr", "fleet", "statistics", "settings"]),
    ("المنصة", ["platform"]),
]
BUSINESS_GROUPS = [
    ("المبيعات والعملاء", ["selling", "crm", "pos"]),
    ("المشتريات والعمليات", ["buying", "stock", "manufacturing"]),
    ("المشاريع والخدمات", ["projects", "support"]),
    ("المالية", ["accounts", "einvoice"]),
    ("الموارد البشرية", ["hr"]),
    ("النظام", ["settings", "platform"]),
]


def _grouped(modules, groups):
    """ترتيب الوحدات حسب مجموعاتها مع وسم كل وحدة بعنوان مجموعتها؛ ما لم يُصنف يأتي آخراً."""
    position = {key: (index, order) for index, (_, keys) in enumerate(groups) for order, key in enumerate(keys)}
    titles = {key: title for title, keys in groups for key in keys}
    for module in modules:
        module["group"] = titles.get(module["key"], "")
    return sorted(modules, key=lambda m: position.get(m["key"], (len(groups), 0)))


def _keys(module):
    # الوحدة المدمجة (كالشؤون المالية للجمعية) تظهر لمن يملك صلاحية أي وحدة من مكوناتها
    return module.get("keys", (module["key"],))


# ترتيب وحدات الجهة غير الربحية
NONPROFIT_ORDER = ["office", "members", "hr", "documents", "accounts", "resources", "grants", "awqaf", "projects", "entities", "beneficiaries", "governance",
                   "volunteering", "stock", "programs", "quran", "dawah", "health", "excellence", "relations", "fleet", "statistics", "website", "store", "settings"]


def nonprofit_modules(modules, doc, company):
    """قائمة الجهة غير الربحية: الوحدات بمسميات أنظمة الجمعيات، دون الوحدات التجارية، مع وحدات المستفيدين."""
    from core.permissions import COMMERCIAL_MODULES, NONPROFIT_LABELS

    by_key = {m["key"]: m for m in modules}
    accounts = by_key["accounts"]
    accounts["keys"] = ("accounts", "funds", "buying", "einvoice")
    buying = by_key.pop("buying", None)
    if buying:
        titles = {"التقارير": "تقارير الموردين"}
        accounts["sections"][1:1] = [{**s, "title": titles.get(s["title"], s["title"])} for s in buying["sections"]]
    for section in accounts["sections"]:
        if section.get("title") == "القوائم المالية":
            section["items"].insert(0, _item("حزمة القوائم المالية للجمعية (للمراجع)", "statements_package"))
    accounts["sections"].insert(1, {"title": "الأموال المقيدة", "items": [
        doc("الصناديق والأموال المقيدة", "fund"),
        _item("أرصدة الصناديق", "fund_balances"), _item("قائمة الأنشطة", "statement_of_activities")]})
    einvoice = by_key.pop("einvoice", None)
    if einvoice:
        accounts["sections"].append({"title": "الفوترة الإلكترونية", "items": einvoice["sections"][0]["items"]})
    by_key["beneficiaries"] = {"key": "beneficiaries", "label": "إدارة التقييم والمتابعة", "sections": [
        {"title": "المستفيدون", "items": [_item("لوحة المتابعة", "beneficiaries_dashboard"),
                                         doc("ملفات المستفيدين", "beneficiary"), doc("دراسات الحالة", "case-assessment")]},
        {"title": "المساعدات", "items": [doc("صرف المساعدات", "aid-disbursement"), doc("برامج المساعدات", "aid-program")]},
        {"title": "لجنة المساعدات", "items": [_item("قرارات اللجنة", "committee_page"),
                                             doc("تشكيل اللجنة", "aid-committee")]},
        {"title": "القسائم الشرائية", "items": [doc("القسائم", "aid-voucher"), doc("عمليات القسائم", "voucher-redemption"),
                                               doc("المتاجر الشريكة", "voucher-merchant")]},
        {"title": "بوابة المستفيد", "items": [doc("طلبات تحديث البيانات", "portal-profile-update"),
                                             doc("طلبات الخدمة", "portal-service-request"), doc("المواعيد المتاحة", "service-slot"), doc("حجوزات الخدمات", "service-booking")]},
        {"title": "الإعداد", "items": [doc("فئات المستفيدين", "beneficiary-category"), doc("تصنيفات وحالات التابعين", "dependent-category"),
                                      doc("معايير دراسة الحالة", "assessment-criterion")]},
    ]}
    by_key["resources"] = {"key": "resources", "label": "إدارة الموارد المالية", "sections": [
        {"title": "التبرعات", "items": [_item("لوحة الموارد المالية", "resources_dashboard"),
                                       doc("سندات التبرعات", "donation-receipt"),
                                       doc("التبرعات العينية", "in-kind-donation"), doc("وعود التبرع", "pledge"),
                                       doc("الداعمون", "donor")]},
        {"title": "الكفالات", "items": [doc("الكفالات", "sponsorship"), doc("استحقاقات الكفالات", "sponsorship-due"),
                                       doc("أنواع الكفالات", "sponsorship-type")]},
        {"title": "حملات التواصل", "items": [_item("لوحة الحملات", "campaigns_dashboard"), doc("الحملات", "campaign"),
                                            doc("الشرائح", "campaign-segment"),
                                            doc("قائمة الإيقاف", "campaign-optout"),
                                            _item("إعدادات التذكير", "campaigns_settings")]},
    ]}
    by_key["members"] = {"key": "members", "label": "إدارة الأعضاء المشاركين", "sections": [
        {"title": "الجمعية العمومية", "items": [_item("لوحة الأعضاء", "members_dashboard"), doc("الأعضاء", "member"),
                                               doc("اشتراكات الأعضاء", "membership-fee"),
                                               doc("فئات العضوية", "membership-category")]},
        {"title": "المجلس والاجتماعات", "items": [doc("دورات مجلس الإدارة", "board-term"),
                                                 doc("الاجتماعات والمحاضر", "meeting"),
                                                 doc("القرارات ومتابعة تنفيذها", "meeting-decision")]},
    ]}
    by_key["governance"] = {"key": "governance", "label": "إدارة الحوكمة", "sections": [
        {"items": [_item("لوحة الحوكمة", "governance_dashboard"), doc("التقييم الذاتي", "governance-assessment"),
                   doc("مؤشرات الحوكمة", "governance-indicator"), doc("معايير الحوكمة", "governance-standard"),
                   _item("تقرير الجاهزية لتقييم الحوكمة", "governance_readiness")]},
    ]}
    by_key["grants"] = {"key": "grants", "label": "إدارة المنح", "sections": [
        {"items": [_item("لوحة المنح", "grants_dashboard"), doc("طلبات المنح", "grant-application"),
                   doc("المنح المعتمدة", "grant"), doc("دفعات المنح", "grant-installment"),
                   doc("تقارير المانحين", "grant-report"), doc("الجهات المانحة", "donor")]},
    ]}
    by_key["awqaf"] = {"key": "awqaf", "label": "إدارة الأوقاف", "sections": [
        {"items": [_item("لوحة الأوقاف", "awqaf_dashboard"), doc("الأوقاف", "waqf"), doc("عقود الإيجار", "waqf-lease"),
                   doc("أقساط الإيجار", "rent-installment"), doc("سندات قبض الريع", "rent-receipt"),
                   doc("توزيع الغلة", "waqf-distribution")]},
    ]}
    by_key["volunteering"] = {"key": "volunteering", "label": "إدارة التطوع", "sections": [
        {"items": [_item("لوحة التطوع", "volunteering_dashboard"), doc("الفرص التطوعية", "volunteer-opportunity"),
                   doc("المتطوعون", "volunteer"), doc("المشاركات", "volunteer-participation")]},
    ]}
    by_key["office"] = {"key": "office", "label": "المكتب الإلكتروني", "sections": [
        {"items": [_item("مكتبي", "my_desk"), doc("الطلبات الإدارية", "admin-request"), doc("التعاميم", "circular"),
                   doc("أنواع الطلبات الإدارية", "admin-request-type")]},
    ]}
    by_key["documents"] = {"key": "documents", "label": "إدارة التوثيق والمستندات", "sections": [
        {"title": "الصادر والوارد", "items": [
            _item("لوحة المعاملات", "documents_dashboard"),
            _item("الوارد", "doc_list", "correspondence", query="?direction=incoming"),
            _item("الصادر", "doc_list", "correspondence", query="?direction=outgoing"),
            _item("المعاملات الداخلية", "doc_list", "correspondence", query="?direction=internal")]},
        {"title": "الأرشيف", "items": [doc("الأرشيف", "archive-document"), doc("تصنيفات الأرشيف", "archive-category")]},
    ]}
    by_key["entities"] = {"key": "entities", "label": "إدارة الجهات المستفيدة", "sections": [
        {"items": [_item("لوحة الطلبات", "entities_dashboard"), doc("الجهات", "beneficiary-entity"),
                   doc("طلبات الجهات", "entity-request")]},
    ]}
    by_key["programs"] = {"key": "programs", "label": "إدارة البرامج والتطوير", "sections": [
        {"items": [_item("لوحة البرامج", "programs_dashboard"), doc("البرامج التدريبية", "training-program"),
                   doc("المتدربون", "program-enrollment"), _item("تقرير قياس الأثر", "program_impact"), _item("لوحة النتائج والتكلفة", "impact_center")]},
    ]}
    by_key["excellence"] = {"key": "excellence", "label": "إدارة التميز المؤسسي", "sections": [
        {"title": "الخطة الاستراتيجية", "items": [_item("لوحة التميز المؤسسي", "excellence_dashboard"),
                                                 doc("الخطط الاستراتيجية", "strategic-plan"),
                                                 doc("الأهداف الاستراتيجية", "strategic-goal")]},
        {"title": "الخطة التشغيلية", "items": [doc("المبادرات والمشاريع", "initiative")]},
        {"title": "قياس الأداء", "items": [doc("مؤشرات الأداء", "kpi"), _item("إدخال قراءات المؤشرات", "kpi_entry"),
                                     _item("تقرير الأداء المؤسسي", "performance_report")]},
    ]}
    by_key["relations"] = {"key": "relations", "label": "إدارة العلاقات العامة والإعلام", "sections": [
        {"title": "العلاقات", "items": [_item("لوحة العلاقات العامة", "relations_dashboard"), doc("الشراكات", "partnership"),
                                       doc("جهات التواصل", "pr-contact"), doc("الزيارات", "visit")]},
        {"title": "الإعلام والفعاليات", "items": [doc("الفعاليات والحملات", "pr-event"),
                                                 doc("التغطيات الإعلامية", "media-coverage"),
                                                 doc("الاستطلاعات", "survey"), _item("تقرير العلاقات العامة", "relations_report")]},
    ]}
    by_key["fleet"] = {"key": "fleet", "label": "إدارة الحركة والصيانة", "sections": [
        {"title": "الحركة", "items": [_item("لوحة الحركة والصيانة", "fleet_dashboard"), doc("طلبات السيارات", "vehicle-request"),
                                     doc("السيارات", "vehicle"), doc("سجل الوقود", "fuel-log")]},
        {"title": "الصيانة", "items": [doc("طلبات الصيانة", "maintenance-request"), _item("تقرير الحركة والصيانة", "fleet_report")]},
    ]}
    by_key["website"] = {"key": "website", "label": "إدارة الموقع الإلكتروني", "sections": [
        {"items": [_item("لوحة الموقع", "website_dashboard"), doc("الأخبار", "news-post"), doc("ألبومات الصور", "album"),
                   doc("وثائق الحوكمة والإفصاح", "published-document"), doc("شركاء النجاح", "partner"),
                   _item("إعدادات الموقع", "website_settings")]},
        {"title": "محتوى الواجهة", "items": [doc("شرائح الواجهة", "hero-slide"), doc("أرقام الأثر", "impact-counter"),
                                            doc("الفيديوهات", "video"), doc("الأسئلة الشائعة", "faq"),
                                            doc("مشتركو النشرة البريدية", "newsletter-subscriber")]},
    ]}
    by_key["store"] = {"key": "store", "label": "المتجر الإلكتروني", "sections": [
        {"items": [_item("لوحة المتجر", "store_dashboard"), doc("فرص التبرع", "donation-opportunity"),
                   doc("طلبات التبرع الإلكتروني", "donation-order"), doc("التبرعات الشهرية", "recurring-donation"),
                   doc("المسوّقون والسفراء", "marketer"), _item("إعدادات المتجر وبوابة الدفع", "store_settings")]},
    ]}
    by_key["statistics"] = {"key": "statistics", "label": "إدارة التقارير والإحصائيات", "sections": [
        {"items": [_item("مركز التقارير والإحصائيات", "statistics_center")]},
    ]}
    specialties = {
        "quran": ("الحلقات ومتابعة الحفظ", [("حلقات التحفيظ", "quran-circle"), ("طلاب الحلقات", "quran-student"), ("متابعة الحفظ والمراجعة", "quran-progress"), ("خطط الحفظ الفردية", "quran-learning-plan")]),
        "dawah": ("الدعوة والمسلمون الجدد والعمرة", [("المسلمون الجدد", "new-muslim"), ("البرامج الدعوية", "dawah-activity"), ("رحلات العمرة", "umrah-trip"), ("متابعات المسلمين الجدد", "dawah-follow-up"), ("تجهيزات رحلات العمرة", "umrah-checklist"), ("المشاركون في العمرة", "umrah-participant")]),
        "health": ("الخدمات الصحية", [("المرضى", "health-patient"), ("المواعيد الصحية", "health-appointment"), ("الإحالات الصحية", "health-referral")]),
    }
    for key, (label, links) in specialties.items():
        if key == company.association_specialty:
            by_key[key] = {"key": key, "label": label, "sections": [{"items": [_item("لوحة المتابعة وطلبات التسجيل", "specialty_dashboard"), _item("لوحة الأثر والنتائج", "impact_center")] + [doc(title, slug) for title, slug in links]}]}
    by_key["office"]["sections"].insert(0, {"title": "الهيكل والصلاحيات", "items": [_item("الهيكل الإداري والتنفيذي", "organization_chart"), doc("أدوار صلاحيات الجمعية", "association-role")]})
    for key, label in NONPROFIT_LABELS.items():
        if key in by_key:
            by_key[key]["label"] = label
    return [by_key[key] for key in NONPROFIT_ORDER if key in by_key and key not in COMMERCIAL_MODULES]


# الوحدة التي لا تتجاوز عناصرها هذا العدد تُعرض أقسامها عناوين ثابتة دون طي، والأكبر تُطوى أقسامها
FLAT_LIMIT = 30


def _mark_active(request, modules):
    # تحديد العنصر النشط (الأطول تطابقاً) وفتح وحدته
    path = request.get_full_path()
    best, best_len = None, 0
    for module in modules:
        module["icon"] = ICONS[module["key"]]
        module["flat"] = sum(len(s["items"]) for s in module["sections"]) <= FLAT_LIMIT
        for section in module["sections"]:
            for item in section["items"]:
                base = item["url"].split("?")[0]
                exact = item["url"] == path
                if (exact or path.startswith(base)) and (len(item["url"]) if exact else len(base)) > best_len:
                    best, best_len = (module, item), (len(item["url"]) + 1000 if exact else len(base))
    if best:
        best[0]["open"] = True
        best[1]["active"] = True
    for module in modules:
        # القسم الذي فيه الصفحة الحالية مفتوح، وإلا فالقسم الأول من الوحدة
        sections = module["sections"]
        active = next((s for s in sections if any(i.get("active") for i in s["items"])), None)
        (active or sections[0])["open"] = True
    return modules


def breadcrumb(modules, path):
    """مسار الصفحة الحالية في القائمة: [(النص، الرابط)] — الوحدة، ثم القسم، ثم صفحة القائمة إن كنا داخل أحد عناصرها."""
    for module in modules:
        for section in module["sections"]:
            for item in section["items"]:
                if not item.get("active"):
                    continue
                trail = [(module["label"], module["sections"][0]["items"][0]["url"])]
                if section.get("title") and len(module["sections"]) > 1:
                    trail.append((section["title"], section["items"][0]["url"]))
                if item["url"].split("?")[0] != path:
                    trail.append((item["label"], item["url"]))
                return trail
    return []


def menu_pages(modules):
    """كل صفحات القائمة المتاحة للمستخدم، للبحث العام: [{label, url, module, section}]."""
    return [{"label": item["label"], "url": item["url"], "module": module["label"], "section": section.get("title") or ""}
            for module in modules for section in module["sections"] for item in section["items"]]
