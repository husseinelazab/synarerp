"""إرسال الحملات والتذكيرات الآلية.

- الإرسال SMS عبر messaging.service.send بوضع provider فقط (لا روابط واتساب في الحملات).
  بدون مزوّد مفعّل يُسجل المستلم «فشلت» بسبب واضح ولا يتعطل شيء.
- البريد عبر core.mail.send، ويُلحق به رابط إلغاء اشتراك موقّع /u/<token>/.
- الإرسال متكرر بأمان: كل مستلم (حملة، قناة، عنوان) سطر واحد، ويُحجز قبل الإرسال فلا يُرسل له مرتين.

الدوال للمجدول:
    campaigns.service.run_scheduled_campaigns(company)
    campaigns.service.campaigns_due_reminders(company)
"""
import logging
import math

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.tenancy import tenant_context

log = logging.getLogger(__name__)

UNSUBSCRIBE_SALT = "campaigns.unsubscribe"
SMS_FOOTER = "للإيقاف أرسل: إيقاف"
NO_SMS_PROVIDER = "لا يوجد مزوّد رسائل SMS مفعّل — فعّله من الإعدادات ← الرسائل."
NO_MAIL = "البريد الإلكتروني غير مفعّل — اضبط خادم البريد من الإعدادات."
OPTED_OUT = "ضمن قائمة الإيقاف"

GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
           "¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€")


# ───────────────────────── أدوات ─────────────────────────

def _country_code(company):
    if company is None:
        return "966"
    cached = getattr(company, "_campaign_cc", None)
    if cached is None:
        from messaging.models import MessagingSettings

        cached = (MessagingSettings.all_objects.filter(company=company).values_list("country_code", flat=True)
                  .first()) or "966"
        company._campaign_cc = cached
    return cached


def norm_phone(raw, company=None):
    from messaging.service import normalize_phone

    return normalize_phone(raw, _country_code(company)) if raw else ""


def norm_email(raw):
    return (raw or "").strip().lower()


def sms_segments(text):
    """طول الرسالة وعدد أجزائها: العربية UCS-2 (70 حرفاً، 67 للرسالة المتعددة)، اللاتينية GSM-7 (160/153)."""
    gsm = all(ch in GSM7 or ch in GSM7_EXT for ch in text)
    length = sum(2 if ch in GSM7_EXT else 1 for ch in text) if gsm else len(text)
    single, multi = (160, 153) if gsm else (70, 67)
    segments = 0 if not length else 1 if length <= single else math.ceil(length / multi)
    return {"length": length, "segments": segments, "encoding": "GSM-7" if gsm else "UCS-2",
            "single": single, "multi": multi}


def render(text, name, company, link=""):
    return (text.replace("{الاسم}", (name or "").strip() or "داعمنا الكريم")
            .replace("{الجمعية}", company.name).replace("{الرابط}", link or "")).strip()


def site_base(company):
    from core.mail import active_config

    config = active_config(company)
    return ((config.site_url if config else "") or getattr(settings, "SYNAR_SITE_URL", "") or "").rstrip("/")


def campaign_link(campaign):
    if campaign.link:
        return campaign.link
    if campaign.opportunity_id:
        from store.service import site_link

        url = site_link(campaign.company, f"donate/{campaign.opportunity_id}/")
        return url if url.startswith("http") else f"{site_base(campaign.company)}{url}" if url else ""
    return ""


def unsubscribe_token(company, email):
    return signing.dumps({"c": company.pk, "e": norm_email(email)}, salt=UNSUBSCRIBE_SALT, compress=True)


def read_unsubscribe_token(token):
    """يعيد (رقم الشركة، البريد) أو يرفع signing.BadSignature."""
    data = signing.loads(token, salt=UNSUBSCRIBE_SALT)
    return data["c"], data["e"]


def unsubscribe_url(company, email):
    return f"{site_base(company)}/u/{unsubscribe_token(company, email)}/"


def opted_out(company):
    from campaigns.models import OptOut

    rows = OptOut.all_objects.filter(company=company).values_list("phone", "email")
    return {p for p, _ in rows if p}, {e for _, e in rows if e}


def sms_available(company):
    from messaging.models import MessagingSettings

    return MessagingSettings.for_company(company).active


def mail_available(company):
    from core.mail import mail_enabled

    return mail_enabled(company)


# ───────────────────────── الإرسال الفعلي ─────────────────────────

def _send_sms(company, phone, body, reference, event="campaign"):
    """يعيد (نجاح، الخطأ، سجل الرسالة)."""
    from messaging import service as messaging

    try:
        entry, _ = messaging.send(company, phone, body, event=event, reference=reference, mode="provider")
    except ValueError as exc:
        return False, str(exc)[:500], None
    except Exception as exc:  # noqa: BLE001 — فشل مستلم لا يوقف الحملة
        log.exception("تعذر إرسال رسالة الحملة %s", reference)
        return False, f"تعذر الإرسال: {exc}"[:500], None
    if entry is not None and getattr(entry, "status", "") == "failed":
        return False, entry.error or "فشل الإرسال لدى المزوّد", entry
    return True, "", entry


def _send_email(company, email, subject, body):
    from core.mail import send

    footer = f"\n\n—\nلإيقاف رسائلنا البريدية: {unsubscribe_url(company, email)}"
    try:
        ok = send(company, subject, body + footer, [email])
    except Exception as exc:  # noqa: BLE001
        log.exception("تعذر إرسال بريد الحملة")
        return False, f"تعذر الإرسال: {exc}"[:500]
    return (True, "") if ok else (False, NO_MAIL)


def sms_body(campaign, name, link):
    from campaigns.models import CampaignSettings

    body = render(campaign.message, name, campaign.company, link)
    if CampaignSettings.for_company(campaign.company).sms_optout_footer:
        body = f"{body}\n{SMS_FOOTER}"
    return body


# ───────────────────────── الحملات ─────────────────────────

def prepare(campaign):
    """ينشئ أسطر المستلمين من الشريحة (دون تكرار ما أُنشئ سابقاً). يعيد عدد الجديد."""
    from campaigns.models import CampaignRecipient, Channel

    company = campaign.company
    phones_out, emails_out = opted_out(company)
    channels = ["sms", "email"] if campaign.channel == Channel.BOTH else [campaign.channel]
    existing = set(CampaignRecipient.all_objects.filter(campaign=campaign).values_list("channel", "address"))
    new = []
    from campaigns.segments import evaluate

    for row in evaluate(campaign.segment):  # تقييم حي في كل تشغيل (لا نسخة مخزنة)
        for channel in channels:
            address = row["phone"] if channel == "sms" else row["email"]
            if not address or (channel, address) in existing:
                continue
            existing.add((channel, address))
            skipped = address in (phones_out if channel == "sms" else emails_out)
            new.append(CampaignRecipient(company=company, campaign=campaign, channel=channel, address=address,
                                         name=row["name"][:150], source=row["source"][:60],
                                         status="skipped" if skipped else "queued",
                                         error=OPTED_OUT if skipped else ""))
    CampaignRecipient.all_objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


def send_campaign(campaign):
    """يرسل لكل مستلم لم يُحاول بعد. آمن لإعادة التشغيل. يعيد إحصاءات الحملة."""
    from campaigns.models import CampaignRecipient, CampaignStatus

    company = campaign.company
    with tenant_context(company):
        prepare(campaign)
        if campaign.status != CampaignStatus.SENDING:
            campaign.status = CampaignStatus.SENDING
            campaign.save(update_fields=["status"])
        phones_out, emails_out = opted_out(company)
        sms_ok, mail_ok = sms_available(company), mail_available(company)
        link = campaign_link(campaign)
        subject = campaign.email_subject or campaign.title
        reference = f"CMP-{campaign.pk}"
        pending = CampaignRecipient.all_objects.filter(campaign=campaign, status="queued", attempted_at__isnull=True)
        for r in pending.order_by("id"):
            # حجز السطر قبل الإرسال: من يحجزه أولاً يرسل، والباقي يتجاوزه
            if not CampaignRecipient.all_objects.filter(pk=r.pk, attempted_at__isnull=True).update(
                    attempted_at=timezone.now()):
                continue
            entry = None
            if r.address in (phones_out if r.channel == "sms" else emails_out):
                status, error = "skipped", OPTED_OUT
            elif r.channel == "sms":
                if not sms_ok:
                    ok, error = False, NO_SMS_PROVIDER
                else:
                    ok, error, entry = _send_sms(company, r.address, sms_body(campaign, r.name, link), reference)
                status = "sent" if ok else "failed"
            else:
                if not mail_ok:
                    ok, error = False, NO_MAIL
                else:
                    ok, error = _send_email(company, r.address, subject, render(campaign.message, r.name, company,
                                                                                 link))
                status = "sent" if ok else "failed"
            CampaignRecipient.all_objects.filter(pk=r.pk).update(
                status=status, error=error, message_log=entry if entry is not None and entry.pk else None)
        campaign.status = CampaignStatus.SENT
        campaign.sent_at = campaign.sent_at or timezone.now()
        campaign.save(update_fields=["status", "sent_at"])
        return campaign.stats


def sync_statuses(campaign):
    """الرسائل تُسلَّم للمزوّد في الخلفية؛ ما فشل لاحقاً في سجل الرسائل يُنقل للمستلم."""
    from campaigns.models import CampaignRecipient

    rows = CampaignRecipient.all_objects.filter(campaign=campaign, status="sent", message_log__status="failed")
    for r in rows.select_related("message_log"):
        CampaignRecipient.all_objects.filter(pk=r.pk).update(status="failed",
                                                             error=r.message_log.error or "فشل لدى المزوّد")


def send_test(campaign, target):
    """رسالة تجريبية لجوال أو بريد واحد بنص الحملة. يعيد (نجاح، وصف)."""
    company = campaign.company
    target = (target or "").strip()
    link = campaign_link(campaign)
    if "@" in target:
        if not mail_available(company):
            return False, NO_MAIL
        ok, error = _send_email(company, target, f"[تجربة] {campaign.email_subject or campaign.title}",
                                render(campaign.message, "", company, link))
        return ok, f"أُرسل البريد التجريبي إلى {target}." if ok else error
    phone = norm_phone(target, company)
    if not phone:
        return False, "أدخل جوالاً أو بريداً صحيحاً."
    if not sms_available(company):
        return False, NO_SMS_PROVIDER
    ok, error, _ = _send_sms(company, phone, sms_body(campaign, "", link), f"CMP-{campaign.pk}-TEST")
    return ok, f"أُرسلت الرسالة التجريبية إلى {phone}." if ok else error


def run_scheduled_campaigns(company, now=None):
    """للمجدول: يرسل الحملات المجدولة التي حان موعدها ويستكمل المتوقفة أثناء الإرسال. يعيد عدد الحملات."""
    from django.db.models import Q

    from campaigns.models import Campaign, CampaignStatus

    now = now or timezone.now()
    count = 0
    with tenant_context(company):
        due = Campaign.all_objects.filter(company=company).filter(
            Q(status=CampaignStatus.SCHEDULED, scheduled_at__lte=now) | Q(status=CampaignStatus.SENDING))
        for campaign in due.order_by("scheduled_at", "id"):
            try:
                send_campaign(campaign)
                count += 1
            except Exception:  # noqa: BLE001 — حملة معطوبة لا توقف البقية
                log.exception("تعذر إرسال الحملة %s", campaign.pk)
        for campaign in Campaign.all_objects.filter(company=company, status=CampaignStatus.SENT,
                                                    sent_at__gte=now - timezone.timedelta(days=3)):
            sync_statuses(campaign)
    return count


# ───────────────────────── التذكيرات الآلية ─────────────────────────

def _remind(company, key, phone, body):
    """يرسل مرة واحدة لكل مفتاح. يعيد True إن أُرسل الآن."""
    from campaigns.models import ReminderLog

    try:
        with transaction.atomic():
            row = ReminderLog.all_objects.create(company=company, key=key[:80], phone=phone)
    except IntegrityError:
        return False
    ok, error, _ = _send_sms(company, phone, body, key[:60], event="reminder")
    if not ok:
        ReminderLog.all_objects.filter(pk=row.pk).update(status="failed", error=error)
    return ok


def _money(value):
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"


def campaigns_due_reminders(company, today=None):
    """للمجدول (يومياً): تذكير الكافلين قبل موعد الكفالة الشهرية، وتذكير شهري بالمتأخرات. يعيد عدد المُرسل.

    لا يتكرر التذكير لنفس الداعم في نفس الفترة، ولا يُرسل لمن في قائمة الإيقاف، ولا يعمل بدون مزوّد مفعّل.
    """
    from campaigns.models import CampaignSettings
    from funds.models import DueStatus, Sponsorship, SponsorshipDue, SponsorshipStatus, add_months

    cs = CampaignSettings.for_company(company)
    if not (cs.due_reminder_enabled or cs.overdue_reminder_enabled) or not company.is_nonprofit:
        return 0
    if not sms_available(company):
        return 0
    today = today or timezone.localdate()
    phones_out, _ = opted_out(company)
    sent = 0
    with tenant_context(company):
        if cs.due_reminder_enabled:
            period = add_months(today.replace(day=1), 1)
            if (period - today).days <= cs.due_days_before:
                settled = set(SponsorshipDue.all_objects.filter(
                    company=company, period=period, status__in=[DueStatus.PAID, DueStatus.WAIVED])
                    .values_list("sponsorship_id", flat=True))
                by_donor = {}
                for s in (Sponsorship.all_objects.filter(company=company, status=SponsorshipStatus.ACTIVE)
                          .select_related("donor")):
                    if s.pk in settled or not s.covers(period):
                        continue
                    by_donor.setdefault(s.donor, []).append(s)
                for donor, items in by_donor.items():
                    phone = norm_phone(donor.phone, company)
                    if not phone or phone in phones_out:
                        continue
                    amount = sum(s.monthly_amount for s in items)
                    body = (cs.due_text.replace("{الاسم}", donor.name).replace("{الجمعية}", company.name)
                            .replace("{المبلغ}", _money(amount)).replace("{الشهر}", f"{period:%Y-%m}"))
                    sent += _remind(company, f"due:{donor.pk}:{period:%Y-%m}", phone, body)
        if cs.overdue_reminder_enabled:
            month = today.replace(day=1)
            by_donor = {}
            for due in (SponsorshipDue.all_objects.filter(company=company, period__lt=month,
                                                          status__in=[DueStatus.PENDING, DueStatus.PARTIAL])
                        .select_related("sponsorship__donor")):
                by_donor.setdefault(due.sponsorship.donor, []).append(due)
            for donor, dues in by_donor.items():
                phone = norm_phone(donor.phone, company)
                if not phone or phone in phones_out:
                    continue
                amount = sum(d.outstanding for d in dues)
                body = (cs.overdue_text.replace("{الاسم}", donor.name).replace("{الجمعية}", company.name)
                        .replace("{المبلغ}", _money(amount)))
                sent += _remind(company, f"overdue:{donor.pk}:{month:%Y-%m}", phone, body)
    return sent


__all__ = ["send_campaign", "send_test", "run_scheduled_campaigns", "campaigns_due_reminders", "sms_segments",
           "render", "unsubscribe_url", "read_unsubscribe_token", "ValidationError"]
