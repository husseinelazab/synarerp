"""خطوات البداية للمنشأة الجديدة: قائمة في لوحة التحكم تُعلَّم كل خطوة فيها تلقائياً عند إنجازها."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import DocStatus


def _step(title, text, done, url, action):
    return {"title": title, "text": text, "done": bool(done), "url": url, "action": action}


def setup_steps(company):
    from accounts.models import OpeningAccountBalance, OpeningPartyBalance
    from core.models import PrintSettings
    from stock.models import OpeningStock

    new = lambda slug: reverse("doc_new", args=[slug])  # noqa: E731
    opening = any(model.objects.filter(docstatus=DocStatus.SUBMITTED).exists()
                  for model in (OpeningAccountBalance, OpeningPartyBalance, OpeningStock))
    logo = PrintSettings.objects.filter(company=company).exclude(logo="").exists()
    team = company.memberships.filter(is_active=True).exclude(role="employee").count() > 1

    if company.is_nonprofit:
        from beneficiaries.models import Beneficiary
        from funds.models import DonationReceipt, Donor

        steps = [
            _step("بيانات الجمعية", "رقم الترخيص والعنوان والهاتف، وتظهر في الخطابات والإيصالات والقوائم المالية.",
                  company.license_number and company.address, reverse("company_settings"), "إكمال البيانات"),
            _step("الشعار وشكل الطباعة", "شعار الجمعية على السندات والخطابات والإيصالات.", logo,
                  reverse("print_settings"), "رفع الشعار"),
            _step("الأرصدة الافتتاحية", "أرصدة البنوك والصناديق والأموال المقيدة يوم بدء العمل على النظام.", opening,
                  reverse("opening_wizard"), "إدخال الأرصدة"),
            _step("أول متبرع", "سجل الداعمين ليصلهم الإيصال وكشف الحساب.", Donor.objects.exists(), new("donor"),
                  "إضافة متبرع"),
            _step("أول مستفيد", "ملف رب الأسرة وأفراد أسرته ودخلهم، ثم دراسة الحالة.", Beneficiary.objects.exists(),
                  new("beneficiary"), "إضافة مستفيد"),
            _step("فريق العمل", "أضف زملاءك بأدوار وصلاحيات مناسبة لكل منهم.", team, reverse("user_create"),
                  "إضافة مستخدم"),
            _step("أول سند تبرع", "اعتماد أول تبرع يقيده في حساب الصندوق المناسب تلقائياً.",
                  DonationReceipt.objects.filter(docstatus=DocStatus.SUBMITTED).exists(), new("donation-receipt"),
                  "تسجيل تبرع"),
        ]
    else:
        from einvoice.models import EInvoiceSettings
        from pos.models import POSProfile
        from selling.models import Customer, SalesInvoice
        from stock.models import Item

        # «عميل نقدي» يُنشأ تلقائياً لنقطة البيع فلا يُحسب عميلاً أضافه المستخدم
        customers = Customer.objects.exclude(pk__in=POSProfile.objects.values("default_customer"))

        einvoice = EInvoiceSettings.objects.filter(company=company).first()
        steps = [
            _step("بيانات المنشأة", "الرقم الضريبي والعنوان، وهما شرط للفاتورة الضريبية.",
                  company.tax_id and company.address, reverse("company_settings"), "إكمال البيانات"),
            _step("الشعار وشكل الطباعة", "شعارك وبياناتك البنكية على الفواتير، وطباعتها بالعربية والإنجليزية.",
                  logo, reverse("print_settings"), "رفع الشعار"),
            _step("الأرصدة الافتتاحية", "أرصدة البنوك والعملاء والموردين والمخزون يوم بدء العمل على النظام.", opening,
                  reverse("opening_wizard"), "إدخال الأرصدة"),
            _step("الأصناف", "أضف أصنافك يدوياً أو استوردها دفعة واحدة من Excel.", Item.objects.exists(),
                  reverse("doc_list", args=["item"]), "إضافة الأصناف"),
            _step("العملاء", "أضف عملاءك أو استوردهم من Excel.", customers.exists(),
                  reverse("doc_list", args=["customer"]), "إضافة العملاء"),
            _step("الربط مع الفوترة الإلكترونية", "ربط المنشأة ببوابة فاتورة (المرحلة الثانية) لإرسال الفواتير.",
                  einvoice and einvoice.is_active, reverse("einvoice_settings"), "بدء الربط"),
            _step("فريق العمل", "أضف زملاءك بأدوار وصلاحيات مناسبة لكل منهم.", team, reverse("user_create"),
                  "إضافة مستخدم"),
            _step("أول فاتورة", "إصدار أول فاتورة مبيعات معتمدة.",
                  SalesInvoice.objects.filter(docstatus=DocStatus.SUBMITTED).exists(), new("sales-invoice"),
                  "إنشاء فاتورة"),
        ]
    return steps


def onboarding_context(request):
    """قائمة خطوات البداية لمن يدير المنشأة، ما لم تكتمل كلها أو يُخفها."""
    company, membership = request.company, getattr(request, "membership", None)
    if company is None or membership is None or not membership.can_manage or company.onboarding_dismissed:
        return None
    steps = setup_steps(company)
    done = sum(step["done"] for step in steps)
    if done == len(steps):
        return None
    return {"steps": steps, "done": done, "total": len(steps), "percent": round(done * 100 / len(steps))}


@login_required
@require_POST
def dismiss_onboarding(request):
    membership = getattr(request, "membership", None)
    if request.company is not None and membership is not None and membership.can_manage:
        request.company.onboarding_dismissed = True
        request.company.save(update_fields=["onboarding_dismissed"])
    return redirect("dashboard")
