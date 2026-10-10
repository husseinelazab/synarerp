"""إرسال التنبيهات داخل النظام وبالبريد الإلكتروني.

البريد لا يُرسل إلا إذا ضُبط خادم بريد (SYNAR_EMAIL_HOST)؛ وإلا تبقى التنبيهات داخل النظام فقط.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail

from core.models import Membership, Notification, Role

log = logging.getLogger(__name__)


def managers(company):
    return [m.user for m in Membership.objects.filter(company=company, is_active=True,
                                                      role__in=[Role.OWNER, Role.ADMIN]).select_related("user")]


def notify(company, users, title, url="", body=""):
    users = [u for u in users if u is not None]
    Notification.all_objects.bulk_create([Notification(company=company, user=u, title=title, url=url) for u in users])
    recipients = sorted({u.email for u in users if u.email})
    if recipients and settings.SYNAR_EMAIL_ENABLED:
        link = f"{settings.SYNAR_SITE_URL.rstrip('/')}{url}" if settings.SYNAR_SITE_URL and url else ""
        try:
            send_mail(f"[{settings.ERP_NAME}] {title}", "\n\n".join(filter(None, [body or title, link])),
                      None, recipients, fail_silently=False)
        except Exception:  # فشل البريد لا يوقف العملية؛ التنبيه محفوظ داخل النظام
            log.exception("تعذر إرسال بريد التنبيه")


def notify_managers(company, title, url="", body=""):
    notify(company, managers(company), title, url, body)
