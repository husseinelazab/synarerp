"""منطق المتجر: التحقق من الدفع عبر ميسّر، واعتماد الطلب وإنشاء سندات التبرع، والتبرع الشهري."""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.utils import flt
from store import moyasar
from store.models import DonationOrder, DonationOrderLine, OrderStatus, RecurringDonation, StoreSettings

log = logging.getLogger(__name__)

FAILED_STATUSES = {"failed", "voided", "canceled", "cancelled", "expired"}


def normalize_phone(raw):
    from messaging.service import normalize_phone as _normalize

    return _normalize(raw)


def phone_variants(phone):
    """الصيغ المحتملة لرقم محفوظ في سجل الداعم (9665… أو 05…)."""
    variants = {phone, f"+{phone}"}
    if phone.startswith("966"):
        variants.add("0" + phone[3:])
    return list(variants)


def add_month(date):
    month = date.month % 12 + 1
    year = date.year + (1 if date.month == 12 else 0)
    return date.replace(year=year, month=month, day=min(date.day, 28))


def payment_problem(order, payment):
    """سبب رفض عملية الدفع لهذا الطلب، أو "" إن كانت صحيحة ومدفوعة."""
    if not isinstance(payment, dict):
        return "استجابة غير صالحة من بوابة الدفع."
    metadata = payment.get("metadata") or {}
    if str(metadata.get("order", "")) != order.number:
        return "العملية لا تخص هذا الطلب."
    if (payment.get("currency") or "").upper() != "SAR":
        return "عملة الدفع غير صحيحة."
    try:
        amount = int(payment.get("amount"))
    except (TypeError, ValueError):
        return "مبلغ الدفع غير صالح."
    if amount != order.amount_halalas:
        return "مبلغ الدفع لا يطابق مبلغ الطلب."
    status = payment.get("status")
    if status != "paid":
        message = (payment.get("source") or {}).get("message") or ""
        return f"لم تكتمل عملية الدفع ({status}){' — ' + message if message else ''}."
    return ""


def method_label(payment):
    source = payment.get("source") or {}
    kind = source.get("type", "")
    if kind == "creditcard":
        company = (source.get("company") or "").lower()
        return {"mada": "مدى", "visa": "فيزا", "master": "ماستركارد", "mastercard": "ماستركارد",
                "amex": "أمريكان إكسبريس"}.get(company, "بطاقة")
    return {"applepay": "Apple Pay", "stcpay": "STC Pay", "token": "بطاقة محفوظة",
            "samsungpay": "Samsung Pay"}.get(kind, kind or "دفع إلكتروني")


def fetch_and_finalize(order, payment_id):
    """يجلب العملية من ميسّر بالمفتاح السري ثم يعتمد الطلب. يرفع MoyasarError عند تعذر الاتصال."""
    settings = StoreSettings.for_company(order.company)
    payment = moyasar.fetch_payment(settings.secret_key, payment_id)
    return finalize(order, payment)


def finalize(order, payment):
    """اعتماد الطلب مرة واحدة فقط (قفل الصف). يعيد (الطلب، هل اعتُمد الآن)."""
    with transaction.atomic():
        order = DonationOrder.all_objects.select_for_update().get(pk=order.pk)
        if order.status == OrderStatus.PAID:
            return order, False
        problem = payment_problem(order, payment)
        if problem:
            status = (payment or {}).get("status") if isinstance(payment, dict) else ""
            if status in FAILED_STATUSES or (status == "paid"):
                # مدفوعة لكن لا تطابق (مبلغ/طلب/عملة) أو مرفوضة: لا تُعتمد
                order.status = OrderStatus.FAILED
                order.failure_reason = problem[:255]
                order.payment_id = order.payment_id or str(payment.get("id", ""))[:64]
                order.gateway_response = payment
                order.save(update_fields=["status", "failure_reason", "payment_id", "gateway_response"])
            if status == "paid":
                log.error("عملية ميسّر %s مدفوعة ولا تطابق الطلب %s: %s", payment.get("id"), order.number, problem)
            return order, False
        order.status = OrderStatus.PAID
        order.payment_id = str(payment.get("id", ""))[:64]
        order.payment_method = method_label(payment)[:40]
        order.paid_at = timezone.now()
        order.failure_reason = ""
        order.gateway_response = payment
        order.donor = match_donor(order)
        order.save()
        _create_receipts(order)
        if order.is_monthly and order.recurring_id is None:
            _create_recurring(order, payment)
    _notify(order)
    return order, True


def match_donor(order):
    from funds.models import Donor

    donor = Donor.all_objects.filter(company=order.company, phone__in=phone_variants(order.donor_phone)).first()
    if donor is not None:
        return donor
    name = order.donor_name if order.donor_name and order.donor_name != "فاعل خير" else f"متبرع {order.donor_phone}"
    if Donor.all_objects.filter(company=order.company, name=name).exists():
        name = f"{name} ({order.donor_phone[-4:]})"
        if Donor.all_objects.filter(company=order.company, name=name).exists():
            name = f"{name} {order.number}"
    return Donor.all_objects.create(company=order.company, name=name[:150], phone=order.donor_phone,
                                    email=order.donor_email, notes="أُنشئ من المتجر الإلكتروني")


def _create_receipts(order):
    from funds.models import DonationReceipt, PaymentMethod

    settings = StoreSettings.for_company(order.company)
    problems = []
    for line in order.lines.select_related("opportunity").all():
        opportunity = line.opportunity
        receipt = DonationReceipt(
            company=order.company, donor=order.donor, donor_name=order.display_name, donor_phone=order.donor_phone,
            donation_type=opportunity.donation_type, fund=opportunity.resolve_fund(), amount=line.amount,
            payment_method=PaymentMethod.ONLINE, account=settings.clearing_account,
            reference_no=order.payment_id[:50], posting_date=timezone.localdate(),
            remarks=f"تبرع إلكتروني {order.number} — {opportunity.title}"
                    + (f" (إهداء إلى {order.gift_to_name})" if order.gift_to_name else ""))
        receipt.save()
        line.receipt = receipt
        line.save(update_fields=["receipt"])
        try:
            with transaction.atomic():
                receipt.submit()
        except ValidationError as exc:
            # الدفع تم فعلاً: يبقى السند مسودة للمراجعة ولا يُرفض الطلب
            problems.append(f"{receipt.name}: {' '.join(exc.messages)}")
    if problems:
        try:
            from core.notify import notify_managers

            notify_managers(order.company, f"سندات تبرع إلكتروني لم تُعتمد تلقائياً ({order.number})",
                            order.get_absolute_url(), "\n".join(problems))
        except Exception:  # noqa: BLE001
            log.exception("تعذر تنبيه المديرين")


def _create_recurring(order, payment):
    token = (payment.get("source") or {}).get("token")
    if not token:
        return
    source = payment.get("source") or {}
    first = None
    for line in order.lines.select_related("opportunity"):
        rd = RecurringDonation(company=order.company, donor_name=order.donor_name, donor_phone=order.donor_phone,
                               opportunity=line.opportunity, amount=line.amount, last_order=order,
                               card_label=(source.get("number") or "")[-8:],
                               next_charge_on=add_month(timezone.localdate()))
        rd.token = token
        rd.save()
        first = first or rd
    order.recurring = first
    order.save(update_fields=["recurring"])


def site_link(company, path):
    from website.models import Website

    site = Website.all_objects.filter(company=company).first()
    if site is None:
        return ""
    if site.domain:
        return f"https://{site.domain}/{path}"
    return f"/site/{site.slug}/{path}"


def _send(company, phone, body, event, reference):
    try:
        from messaging.service import send

        send(company, phone, body, event=event, reference=reference)
    except Exception:  # noqa: BLE001 — الرسائل لا تُفشل اعتماد التبرع
        log.exception("تعذر إرسال رسالة المتجر %s", reference)


def _notify(order):
    settings = StoreSettings.for_company(order.company)
    link = site_link(order.company, f"donate/certificate/{order.number}/?c={order.verify_code}")
    body = (f"{settings.thank_you_message or 'شكراً لتبرعكم'}\n"
            f"تبرعك رقم {order.number} بمبلغ {flt(order.total):,.2f} ريال لـ{order.company.name}.")
    if link.startswith("https://"):
        body += f"\nشهادة الشكر: {link}"
    _send(order.company, order.donor_phone, body, "store_thanks", order.number)
    if order.gift_to_phone:
        titles = "، ".join(line.opportunity.title for line in order.lines.select_related("opportunity"))
        gift = (f"{order.gift_to_name or 'عزيزنا'}، أهداك {order.display_name} تبرعاً في «{titles}» "
                f"لدى {order.company.name}.")
        if order.gift_message:
            gift += f"\n{order.gift_message}"
        _send(order.company, order.gift_to_phone, gift, "store_gift", order.number)


# ─────────────────────────── التبرع الشهري ───────────────────────────

def charge_recurring(rd, today=None):
    """يخصم تبرعاً شهرياً مستحقاً بالبطاقة المحفوظة. يعيد الطلب الجديد."""
    today = today or timezone.localdate()
    company = rd.company
    settings = StoreSettings.for_company(company)
    order = DonationOrder.all_objects.create(
        company=company, donor_name=rd.donor_name, donor_phone=rd.donor_phone, total=rd.amount, recurring=rd)
    DonationOrderLine.objects.create(order=order, opportunity=rd.opportunity, quantity=1, amount=rd.amount)
    payload = {"amount": order.amount_halalas, "currency": "SAR",
               "description": f"تبرع شهري {order.number}: {rd.opportunity.title}"[:250],
               "source": {"type": "token", "token": rd.token},
               "metadata": {"order": order.number, "ref": order.reference}}
    callback = site_link(company, "donate/callback/")
    if callback.startswith("https://"):
        payload["callback_url"] = callback
    try:
        payment = moyasar.create_payment(settings.secret_key, payload)
    except moyasar.MoyasarError as exc:
        payment = {"status": "failed", "source": {"message": str(exc)}}
    paid = False
    if isinstance(payment, dict) and payment.get("id") and payment.get("status") == "paid":
        order, _ = finalize(order, payment)
        paid = order.status == OrderStatus.PAID
    if not paid:
        order.refresh_from_db()
        if order.status != OrderStatus.FAILED:
            order.status = OrderStatus.FAILED
            order.failure_reason = (payment_problem(order, payment) if isinstance(payment, dict)
                                    else "تعذر الخصم")[:255]
            order.gateway_response = payment if isinstance(payment, dict) else {}
            order.save(update_fields=["status", "failure_reason", "gateway_response"])
    rd.last_order = order
    if paid:
        rd.failures = 0
        rd.next_charge_on = add_month(today)
    else:
        rd.failures += 1
        rd.next_charge_on = today + timezone.timedelta(days=1)  # يُعاد المحاولة غداً
        if rd.failures == 2:
            try:
                from core.notify import notify_managers

                notify_managers(company, f"تعذر خصم التبرع الشهري للمتبرع {rd.donor_name} مرتين",
                                rd.get_absolute_url(), order.failure_reason)
            except Exception:  # noqa: BLE001
                log.exception("تعذر تنبيه المديرين")
        if rd.failures >= 3:
            rd.active = False
    rd.save(update_fields=["last_order", "failures", "next_charge_on", "active"])
    return order


def charge_due_recurring(today=None):
    """كل التبرعات الشهرية المستحقة في كل الجمعيات. يعيد (المدفوع، الفاشل)."""
    from core.tenancy import tenant_context

    today = today or timezone.localdate()
    paid = failed = 0
    due = (RecurringDonation.all_objects.filter(active=True, next_charge_on__lte=today)
           .select_related("company", "opportunity").order_by("company_id", "id"))
    for rd in due:
        settings = StoreSettings.for_company(rd.company)
        if not settings.ready:
            continue
        with tenant_context(rd.company):
            order = charge_recurring(rd, today)
        if order.status == OrderStatus.PAID:
            paid += 1
        else:
            failed += 1
    return paid, failed


def to_decimal(value):
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except Exception:  # noqa: BLE001
        return None
    return result if result.is_finite() else None
