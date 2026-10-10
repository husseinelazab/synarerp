"""ربط فواتير المبيعات بمنصة فاتورة: الإصدار والتوقيع والإرسال، ومراحل الربط (Onboarding)."""
import base64
import datetime
import re
import uuid as uuidlib
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.utils import allocate, flt
from einvoice import crypto
from einvoice.api import ZatcaClient, ZatcaError, error_messages
from einvoice.models import (FIRST_INVOICE_PIH, EInvoice, EInvoiceSettings, EInvoiceStatus, EInvoiceType,
                             OnboardingStatus)
from einvoice.xml import CREDIT_NOTE, DEBIT_NOTE, INVOICE, InvoiceData, Line, Party, sign_invoice

VAT_RE = re.compile(r"^3\d{13}3$")


def settings_for(company):
    return EInvoiceSettings.all_objects.filter(company=company).first()


def _signing_time():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------- نوع الفاتورة والتحقق ----------------

def einvoice_type(invoice, settings):
    if settings.invoice_types == "1000":
        return EInvoiceType.STANDARD
    if settings.invoice_types == "0100":
        return EInvoiceType.SIMPLIFIED
    if invoice.is_return and invoice.return_against_id:
        original = EInvoice.all_objects.filter(sales_invoice_id=invoice.return_against_id).first()
        if original:
            return original.invoice_type
    if invoice.is_pos or not invoice.customer.tax_id:
        return EInvoiceType.SIMPLIFIED
    return EInvoiceType.STANDARD


def seller_errors(settings):
    errors = []
    if not VAT_RE.match(settings.vat_number or ""):
        errors.append("الرقم الضريبي للبائع يجب أن يكون 15 رقماً يبدأ وينتهي بالرقم 3.")
    if not (settings.crn or "").isdigit():
        errors.append("السجل التجاري للبائع مطلوب (أرقام فقط).")
    if not re.fullmatch(r"\d{4}", settings.building_number or ""):
        errors.append("رقم مبنى البائع يجب أن يكون 4 أرقام.")
    if not re.fullmatch(r"\d{5}", settings.postal_code or ""):
        errors.append("الرمز البريدي للبائع يجب أن يكون 5 أرقام.")
    for field, label in (("street", "الشارع"), ("district", "الحي"), ("city", "المدينة"), ("seller_name", "اسم البائع")):
        if not getattr(settings, field):
            errors.append(f"{label} في عنوان البائع مطلوب.")
    return errors


def invoice_errors(invoice, settings, kind):
    errors = seller_errors(settings)
    if flt(invoice.tax_rate) != flt(15):
        errors.append("الفوترة الإلكترونية تدعم حالياً نسبة الضريبة الأساسية 15% فقط.")
    if kind == EInvoiceType.STANDARD:
        customer = invoice.customer
        if not VAT_RE.match(customer.tax_id or ""):
            errors.append(f"الفاتورة الضريبية تتطلب رقماً ضريبياً صحيحاً للعميل «{customer}» (15 رقماً يبدأ وينتهي بـ 3).")
        if not re.fullmatch(r"\d{4}", customer.building_number or ""):
            errors.append(f"رقم مبنى العميل «{customer}» مطلوب (4 أرقام).")
        if not re.fullmatch(r"\d{5}", customer.postal_code or ""):
            errors.append(f"الرمز البريدي للعميل «{customer}» مطلوب (5 أرقام).")
        for field, label in (("street", "الشارع"), ("district", "الحي"), ("city", "المدينة")):
            if not getattr(customer, field):
                errors.append(f"{label} في عنوان العميل «{customer}» مطلوب للفاتورة الضريبية.")
    return errors


# ---------------- بناء بيانات الفاتورة ----------------

def seller_party(settings):
    return Party(name=settings.seller_name, vat=settings.vat_number, crn=settings.crn, street=settings.street,
                 building_number=settings.building_number, district=settings.district, city=settings.city,
                 postal_code=settings.postal_code)


def build_invoice_data(invoice, settings, kind, icv, pih, uuid, issued_at):
    rate = Decimal(invoice.tax_rate)
    rows = list(invoice.items.select_related("item").order_by("id"))
    amounts = {row.pk: abs(row.amount) for row in rows}
    discount = abs(invoice.discount_excluding_tax)
    # عند الأسعار الشاملة للضريبة يُوزع الصافي قبل الضريبة على الأسطر
    nets = allocate(abs(invoice.net_total) + discount, amounts) if invoice.prices_include_tax else amounts
    lines = [Line(row.item.name, abs(row.qty), nets[row.pk], flt(nets[row.pk] * rate / 100)) for row in rows]
    customer = invoice.customer
    buyer = Party()
    if kind == EInvoiceType.STANDARD:
        buyer = Party(name=customer.name, vat=customer.tax_id, street=customer.street,
                      building_number=customer.building_number, district=customer.district, city=customer.city,
                      postal_code=customer.postal_code, country=customer.country_code or "SA")
    if invoice.is_pos:
        means = "48" if invoice.paid_card and not invoice.paid_cash else "10"
    else:
        means = "30"
    local = timezone.localtime(issued_at)
    return InvoiceData(
        number=invoice.name, uuid=str(uuid), issue_date=local.strftime("%Y-%m-%d"), issue_time=local.strftime("%H:%M:%S"),
        type_code=CREDIT_NOTE if invoice.is_return else INVOICE, simplified=kind == EInvoiceType.SIMPLIFIED,
        currency=invoice.currency.code if invoice.currency_id else "SAR", icv=icv, pih=pih,
        seller=seller_party(settings), buyer=buyer, lines=lines, tax_rate=rate,
        line_extension=sum((line.net_amount for line in lines), flt(0)), allowance=discount,
        tax_exclusive=abs(invoice.net_total), tax_amount=abs(invoice.tax_amount),
        tax_inclusive=abs(invoice.grand_total), tax_amount_sar=abs(invoice.base_tax_amount or invoice.tax_amount),
        supply_date=invoice.posting_date.strftime("%Y-%m-%d"), payment_means=means,
        billing_reference=invoice.return_against.name if invoice.is_return and invoice.return_against_id else "",
        reason=(invoice.remarks or "إرجاع أصناف") if invoice.is_return else "",
    )


# ---------------- الإصدار والإرسال ----------------

def _production_certificate(settings):
    return crypto.certificate_info(crypto.certificate_body_from_token(settings.production_token))


def generate_einvoice(invoice):
    """إصدار وتوقيع الفاتورة الإلكترونية ضمن معاملة اعتماد الفاتورة (يحجز رقم العداد وهاش السلسلة)."""
    settings = EInvoiceSettings.all_objects.select_for_update().get(company=invoice.company)
    kind = einvoice_type(invoice, settings)
    errors = invoice_errors(invoice, settings, kind)
    if errors:
        raise ValidationError(["تعذر إصدار الفاتورة الإلكترونية:"] + errors)
    icv, pih, uid, issued_at = settings.last_icv + 1, settings.last_hash, uuidlib.uuid4(), timezone.now()
    data = build_invoice_data(invoice, settings, kind, icv, pih, uid, issued_at)
    if data.line_extension - data.allowance != data.tax_exclusive:
        raise ValidationError("إجماليات الفاتورة غير متسقة مع أسطرها؛ تعذر إصدار الفاتورة الإلكترونية.")
    signed = sign_invoice(data, crypto.decrypt(settings.private_key), _production_certificate(settings), _signing_time())
    einvoice = EInvoice.all_objects.create(
        company=invoice.company, sales_invoice=invoice, invoice_type=kind, uuid=uid, icv=icv, previous_hash=pih,
        invoice_hash=signed.invoice_hash, issue_datetime=issued_at, xml=signed.xml, qr=signed.qr,
    )
    settings.last_icv, settings.last_hash = icv, signed.invoice_hash
    settings.save(update_fields=["last_icv", "last_hash"])
    return einvoice


def submit_einvoice(einvoice):
    """إرسال الفاتورة للهيئة: الضريبية للاعتماد (Clearance) والمبسطة للإبلاغ (Reporting). لا يرفع استثناء."""
    settings = settings_for(einvoice.company)
    client = ZatcaClient(settings.environment, settings.production_token, crypto.decrypt(settings.production_secret))
    standard = einvoice.invoice_type == EInvoiceType.STANDARD
    einvoice.attempts += 1
    einvoice.submitted_at = timezone.now()
    try:
        status, payload = (client.clear if standard else client.report)(einvoice.xml, einvoice.invoice_hash, einvoice.uuid)
    except ZatcaError as exc:
        einvoice.status, einvoice.response = EInvoiceStatus.FAILED, {"messages": exc.messages}
        einvoice.save()
        return einvoice
    messages = error_messages(payload)
    if status in (200, 202):
        cleared = payload.get("clearedInvoice") if isinstance(payload, dict) else None
        if cleared:
            einvoice.cleared_xml = base64.b64decode(cleared).decode()
        has_warnings = status == 202 or bool((payload.get("validationResults") or {}).get("warningMessages"))
        einvoice.status = EInvoiceStatus.WARNING if has_warnings else (
            EInvoiceStatus.CLEARED if standard else EInvoiceStatus.REPORTED)
    elif status == 409:
        einvoice.status = EInvoiceStatus.CLEARED if standard else EInvoiceStatus.REPORTED
        messages = messages or ["الفاتورة مُرسلة مسبقاً للهيئة."]
    elif status == 400:
        einvoice.status = EInvoiceStatus.REJECTED
    else:
        einvoice.status = EInvoiceStatus.FAILED
        if status in (401, 403):
            messages = ["بيانات اعتماد الربط مع فاتورة غير صالحة أو منتهية؛ أعد الربط من الإعدادات."] + messages
    einvoice.response = {"http_status": status, "messages": messages, "payload": payload}
    einvoice.save()
    return einvoice


def on_invoice_submitted(invoice):
    settings = settings_for(invoice.company)
    if not settings or not settings.is_active:
        return None
    einvoice = generate_einvoice(invoice)
    if settings.auto_submit:
        pk = einvoice.pk
        transaction.on_commit(lambda: submit_einvoice(EInvoice.all_objects.get(pk=pk)))
    return einvoice


def assert_cancellable(invoice):
    if EInvoice.all_objects.filter(sales_invoice=invoice).exists():
        raise ValidationError("لا يمكن إلغاء فاتورة صدرت إلكترونياً لدى هيئة الزكاة والضريبة والجمارك؛ "
                              "أنشئ مرتجعاً (إشعار دائن) بدلاً من ذلك.")


def submit_pending(company=None):
    queryset = EInvoice.all_objects.filter(status__in=[EInvoiceStatus.PENDING, EInvoiceStatus.FAILED])
    if company is not None:
        queryset = queryset.filter(company=company)
    return [submit_einvoice(e) for e in queryset.order_by("company_id", "icv")]


# ---------------- الربط مع المنصة (Onboarding) ----------------

def start_onboarding(settings, otp):
    """إنشاء المفتاح وطلب الشهادة، والحصول على شهادة الامتثال (Compliance CSID) برمز OTP من بوابة فاتورة."""
    errors = seller_errors(settings)
    if errors:
        raise ValidationError(errors)
    key = crypto.generate_private_key_pem()
    serial = f"1-Synar|2-ERP|3-{uuidlib.uuid4()}"
    csr = crypto.generate_csr_pem(key, crypto.CsrInfo(
        common_name=f"Synar-EGS-{settings.company_id}", serial_number=serial, vat_number=settings.vat_number,
        organization_unit=settings.branch_name, organization_name=settings.seller_name,
        invoice_types=settings.invoice_types,
        location=f"{settings.building_number} {settings.street}, {settings.district}, {settings.city}",
        industry=settings.industry, environment=settings.environment,
    ))
    try:
        payload = ZatcaClient(settings.environment).compliance_csid(csr, otp)
    except ZatcaError as exc:
        raise ValidationError(["رفضت المنصة طلب شهادة الامتثال:"] + exc.messages)
    settings.private_key, settings.csr, settings.egs_serial = crypto.encrypt(key), csr, serial
    settings.compliance_token = payload["binarySecurityToken"]
    settings.compliance_secret = crypto.encrypt(payload["secret"])
    settings.compliance_request_id = str(payload["requestID"])
    settings.production_token = settings.production_secret = ""
    settings.status, settings.compliance_results, settings.enabled = OnboardingStatus.COMPLIANCE, {}, False
    settings.save()
    return settings


def _sample_documents(settings):
    """المستندات التجريبية المطلوبة في فحوص الامتثال حسب أنواع الفواتير المسجلة."""
    buyer = Party(name="شركة اختبار الامتثال", vat="399999999900003", street="شارع الاختبار", building_number="1234",
                  district="حي الاختبار", city="الرياض", postal_code="12345")
    kinds = []
    if settings.invoice_types[0] == "1":
        kinds += [("فاتورة ضريبية", False, INVOICE), ("إشعار دائن ضريبي", False, CREDIT_NOTE),
                  ("إشعار مدين ضريبي", False, DEBIT_NOTE)]
    if settings.invoice_types[1] == "1":
        kinds += [("فاتورة مبسطة", True, INVOICE), ("إشعار دائن مبسط", True, CREDIT_NOTE),
                  ("إشعار مدين مبسط", True, DEBIT_NOTE)]
    today = timezone.localtime()
    for label, simplified, type_code in kinds:
        def make(icv, pih, simplified=simplified, type_code=type_code):
            return InvoiceData(
                number=f"COMPLIANCE-{icv}", uuid=str(uuidlib.uuid4()), issue_date=today.strftime("%Y-%m-%d"),
                issue_time=today.strftime("%H:%M:%S"), type_code=type_code, simplified=simplified, currency="SAR",
                icv=icv, pih=pih, seller=seller_party(settings), buyer=Party() if simplified else buyer,
                lines=[Line("صنف تجريبي", Decimal("1"), Decimal("100.00"), Decimal("15.00"))], tax_rate=Decimal("15"),
                line_extension=Decimal("100"), allowance=Decimal("0"), tax_exclusive=Decimal("100"),
                tax_amount=Decimal("15"), tax_inclusive=Decimal("115"), tax_amount_sar=Decimal("15"),
                supply_date=today.strftime("%Y-%m-%d"), payment_means="10",
                billing_reference="COMPLIANCE-1" if type_code != INVOICE else "",
                reason="اختبار الامتثال" if type_code != INVOICE else "",
            )
        yield label, make


def run_compliance_checks(settings):
    key = crypto.decrypt(settings.private_key)
    certificate = crypto.certificate_info(crypto.certificate_body_from_token(settings.compliance_token))
    client = ZatcaClient(settings.environment, settings.compliance_token, crypto.decrypt(settings.compliance_secret))
    results, pih = [], FIRST_INVOICE_PIH
    for icv, (label, make) in enumerate(_sample_documents(settings), start=1):
        data = make(icv, pih)
        signed = sign_invoice(data, key, certificate, _signing_time())
        try:
            status, payload = client.compliance_check(signed.xml, signed.invoice_hash, data.uuid)
        except ZatcaError as exc:
            status, payload = exc.status, {"message": " ".join(exc.messages)}
        validation = (payload.get("validationResults") or {}).get("status", "") if isinstance(payload, dict) else ""
        passed = status in (200, 202) and validation in ("PASS", "WARNING", "")
        results.append({"label": label, "http_status": status, "passed": passed, "validation": validation,
                        "messages": error_messages(payload)})
        pih = signed.invoice_hash
    all_passed = bool(results) and all(r["passed"] for r in results)
    settings.compliance_results = {"results": results, "checked_at": timezone.now().isoformat()}
    if all_passed:
        settings.status = OnboardingStatus.CHECKED
    settings.save(update_fields=["compliance_results", "status"])
    return all_passed


def activate_production(settings):
    client = ZatcaClient(settings.environment, settings.compliance_token, crypto.decrypt(settings.compliance_secret))
    try:
        payload = client.production_csid(settings.compliance_request_id)
    except ZatcaError as exc:
        raise ValidationError(["رفضت المنصة طلب شهادة الإنتاج:"] + exc.messages)
    settings.production_token = payload["binarySecurityToken"]
    settings.production_secret = crypto.encrypt(payload["secret"])
    settings.status, settings.enabled = OnboardingStatus.ACTIVE, True
    # سلسلة الفواتير الفعلية تبدأ من جديد بعد التفعيل
    settings.last_icv, settings.last_hash = 0, FIRST_INVOICE_PIH
    settings.save()
    return settings
