"""تجهيز الرسائل وإرسالها.

- normalize_phone: يحوّل 05xxxxxxxx إلى 9665xxxxxxxx.
- compose: نص المناسبة من قالب الشركة مع بيانات الفاتورة/العميل.
- deliver: يرسل عبر المزوّد المفعّل (في خيط بعد حفظ العملية)، وإلا يعيد رابط wa.me ليفتحه المستخدم.

ملاحظة: صيغ طلبات المزوّدين مبنية على وثائقهم المنشورة؛ جرّب «رسالة اختبار» بحسابك قبل الاعتماد عليها.
"""
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from core.utils import flt, money
from messaging.models import Event, MessageLog, MessageTemplate, MessagingSettings, Provider

log = logging.getLogger(__name__)
TIMEOUT = 20


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def normalize_phone(raw, country_code="966"):
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = country_code + digits[1:]
    elif len(digits) <= 9:
        digits = country_code + digits
    return digits if 8 <= len(digits) <= 15 else ""


def customer_balance(customer):
    from accounts.models import GLEntry
    from django.db.models import Sum

    agg = GLEntry.objects.filter(customer=customer, is_cancelled=False).aggregate(d=Sum("debit"), c=Sum("credit"))
    return flt((agg["d"] or 0) - (agg["c"] or 0))


def compose(company, event, **ctx):
    ctx.setdefault("company", company.name)
    for key in ("amount", "balance"):
        if key in ctx and not isinstance(ctx[key], str):
            ctx[key] = f"{money(ctx[key])} {company.currency.code if company.currency_id else ''}".strip()
    return MessageTemplate.text_for(company, event).format_map(_Safe(ctx)).strip()


def invoice_context(invoice):
    due = invoice.due_date
    return {"customer": invoice.customer.name, "invoice": invoice.name, "amount": invoice.outstanding_amount
            if invoice.outstanding_amount and invoice.is_submitted else invoice.grand_total,
            "due_date": due.strftime("%Y-%m-%d") if due else "",
            "due_text": f" وتستحق في {due:%Y-%m-%d}" if due else "",
            "days": (timezone.localdate() - due).days if due else 0}


def with_invoice_link(company, body, invoice, request=None):
    """يضيف رابط الفاتورة PDF للرسالة (رابط موقّع يفتحه العميل بلا دخول) ما لم يكن موجوداً فيها."""
    from core.pdf import share_url

    link = share_url(request, company, "sales-invoice", invoice)
    if not link:
        return body
    if "{link}" in body:
        return body.replace("{link}", link)
    return body if link in body else f"{body}\n\nالفاتورة (PDF): {link}"


def statement_context(customer):
    from selling.models import SalesInvoice

    overdue = SalesInvoice.objects.filter(customer=customer, docstatus=1, outstanding_amount__gt=0,
                                          due_date__lt=timezone.localdate())
    total_overdue = sum((i.outstanding_amount for i in overdue), flt(0))
    return {"customer": customer.name, "balance": customer_balance(customer),
            "overdue_text": f"، منها {money(total_overdue)} متأخرة السداد" if total_overdue else ""}


def whatsapp_link(phone, body):
    return f"https://wa.me/{phone}?text={urllib.parse.quote(body)}"


# ---------------- المزوّدون ----------------

def _post(url, data=None, headers=None, form=False):
    if form:
        payload = urllib.parse.urlencode(data).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    else:
        payload = json.dumps(data, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=payload, headers={"Accept": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()[:2000].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:2000].decode("utf-8", "replace")


def _send_whatsapp(cfg, phone, body):
    url = cfg.api_url or f"https://graph.facebook.com/v20.0/{cfg.sender}/messages"
    if cfg.whatsapp_template:
        message = {"type": "template", "template": {
            "name": cfg.whatsapp_template, "language": {"code": cfg.whatsapp_language or "ar"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": body}]}]}}
    else:
        message = {"type": "text", "text": {"preview_url": False, "body": body}}
    return _post(url, {"messaging_product": "whatsapp", "to": phone, **message},
                 {"Authorization": f"Bearer {cfg.token}"})


def _send_unifonic(cfg, phone, body):
    url = cfg.api_url or "https://el.cloud.unifonic.com/rest/SMS/messages"
    return _post(url, {"AppSid": cfg.account, "SenderID": cfg.sender, "Recipient": phone, "Body": body}, form=True)


def _send_taqnyat(cfg, phone, body):
    url = cfg.api_url or "https://api.taqnyat.sa/v1/messages"
    return _post(url, {"recipients": [phone], "body": body, "sender": cfg.sender},
                 {"Authorization": f"Bearer {cfg.token}"})


def _send_msegat(cfg, phone, body):
    url = cfg.api_url or "https://www.msegat.com/gw/sendsms.php"
    return _post(url, {"userName": cfg.account, "numbers": phone, "userSender": cfg.sender, "apiKey": cfg.token,
                       "msg": body})


def _send_http(cfg, phone, body):
    template = cfg.http_body or '{"to": "{to}", "message": "{message}"}'
    esc = lambda v: json.dumps(v, ensure_ascii=False)[1:-1]  # noqa: E731 — قيمة آمنة داخل نص JSON
    payload = template.replace("{to}", esc(phone)).replace("{message}", esc(body)).replace("{sender}", esc(cfg.sender))
    try:
        data = json.loads(payload)
    except ValueError:
        return 0, "قالب جسم الطلب ليس JSON صحيحاً."
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.has_token else {}
    return _post(cfg.api_url, data, headers)


SENDERS = {Provider.WHATSAPP_CLOUD: _send_whatsapp, Provider.UNIFONIC: _send_unifonic,
           Provider.TAQNYAT: _send_taqnyat, Provider.MSEGAT: _send_msegat, Provider.HTTP: _send_http}


def provider_send(cfg, phone, body):
    """يرسل فعلياً ويعيد (نجاح، وصف)."""
    try:
        status, text = SENDERS[cfg.provider](cfg, phone, body)
    except (urllib.error.URLError, OSError) as exc:
        return False, f"تعذر الاتصال بالمزوّد: {exc}"
    ok = 200 <= status < 300
    if ok and cfg.provider == Provider.MSEGAT and '"code":"1"' not in text.replace(" ", "") and "M0000" not in text:
        ok = "1" == text.strip()
    return ok, (f"{status}: {text[:300]}" if not ok else "تم الإرسال")


# ---------------- الواجهة ----------------

def send(company, phone_raw, body, event="", reference="", customer=None, user=None, mode="auto"):
    """mode: auto (المزوّد إن كان مفعّلاً وإلا رابط)، link (رابط واتساب فقط)، provider (المزوّد فقط).

    يعيد (سجل الرسالة، رابط واتساب أو "").
    """
    cfg = MessagingSettings.for_company(company)
    phone = normalize_phone(phone_raw, cfg.country_code)
    if not phone:
        raise ValueError("لا يوجد رقم جوال صحيح للعميل.")
    use_provider = cfg.active and mode != "link"
    if mode == "provider" and not cfg.active:
        raise ValueError("الإرسال عبر المزوّد غير مفعّل. فعّله من الإعدادات ← الرسائل.")
    entry = MessageLog.all_objects.create(
        company=company, event=event, channel=cfg.channel if use_provider else "link", phone=phone, body=body,
        status="queued" if use_provider else "link", reference=reference[:60], customer=customer, user=user)
    if not use_provider:
        return entry, whatsapp_link(phone, body)

    def deliver():
        ok, info = provider_send(cfg, phone, body)
        MessageLog.all_objects.filter(pk=entry.pk).update(status="sent" if ok else "failed",
                                                          error="" if ok else info[:500])
        if not ok:
            log.warning("فشل إرسال رسالة %s: %s", entry.pk, info)

    if getattr(django_settings, "SYNAR_MESSAGING_ASYNC", True):
        transaction.on_commit(lambda: threading.Thread(target=deliver, daemon=True).start())
    else:
        deliver()
        entry.refresh_from_db()
    return entry, ""


def send_test(cfg, phone_raw, user=None):
    """رسالة اختبار بالإعدادات المحفوظة (ولو لم تُفعّل). الإجراء الوحيد الذي يتصل بالمزوّد قبل التفعيل."""
    if not cfg.is_configured:
        return False, "أكمل بيانات المزوّد أولاً."
    phone = normalize_phone(phone_raw, cfg.country_code)
    if not phone:
        return False, "أدخل رقم جوال صحيحاً."
    ok, info = provider_send(cfg, phone, f"رسالة اختبار من {cfg.company.name} عبر سِينار.")
    cfg.last_test_at, cfg.last_test_ok, cfg.last_test_message = timezone.now(), ok, info[:500]
    cfg.save(update_fields=["last_test_at", "last_test_ok", "last_test_message"])
    MessageLog.all_objects.create(company=cfg.company, channel=cfg.channel, phone=phone, body="رسالة اختبار",
                                  status="sent" if ok else "failed", error="" if ok else info[:500], user=user)
    return ok, info


__all__ = ["send", "send_test", "compose", "normalize_phone", "Event"]
