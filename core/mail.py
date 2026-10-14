"""إرسال البريد عبر خادم SMTP تضبطه الشركة أو مدير المنصة من الواجهة.

لا يتصل النظام بأي خادم بريد إلا إذا فُعّل الإرسال في الإعدادات، أو عند ضغط «إرسال رسالة اختبار».
الأولوية: خادم الشركة (إن فُعّل) ← خادم المنصة (إن فُعّل) ← متغيرات البيئة SYNAR_EMAIL_* (للتوافق).
"""
import base64
import hashlib
import logging
import threading
from dataclasses import dataclass
from email.utils import formataddr

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)


def _fernet():
    key = hashlib.sha256((settings.SECRET_KEY + ":synar-mail").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_password(text):
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt_password(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        log.error("تعذر فك تشفير كلمة مرور البريد؛ هل تغيّر SECRET_KEY؟ أعد إدخالها من الإعدادات.")
        return ""


@dataclass
class MailConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    from_email: str
    from_name: str
    site_url: str
    source: str  # company / platform / environment

    def connection(self, timeout=20):
        return get_connection(
            "django.core.mail.backends.smtp.EmailBackend", host=self.host, port=self.port,
            username=self.username or None, password=self.password or None,
            use_tls=self.security == "tls", use_ssl=self.security == "ssl", timeout=timeout, fail_silently=False)

    @property
    def sender(self):
        return formataddr((self.from_name, self.from_email)) if self.from_name else self.from_email


def config_from(obj, source):
    return MailConfig(host=obj.host, port=obj.port, security=obj.security, username=obj.username,
                      password=decrypt_password(obj.password_encrypted), from_email=obj.from_email,
                      from_name=obj.from_name or settings.ERP_NAME, site_url=obj.site_url, source=source)


def active_config(company=None):
    """إعدادات الإرسال الفعّالة للشركة، أو None إن كان البريد معطلاً."""
    from core.models import EmailSettings

    if company is not None:
        own = EmailSettings.objects.filter(company=company, enabled=True).first()
        if own and own.is_configured:
            return config_from(own, "company")
    platform = EmailSettings.objects.filter(company__isnull=True, enabled=True).first()
    if platform and platform.is_configured:
        return config_from(platform, "platform")
    if settings.SYNAR_EMAIL_ENABLED:
        return MailConfig(host=settings.EMAIL_HOST, port=settings.EMAIL_PORT,
                          security="tls" if settings.EMAIL_USE_TLS else "none", username=settings.EMAIL_HOST_USER,
                          password=settings.EMAIL_HOST_PASSWORD, from_email=settings.DEFAULT_FROM_EMAIL,
                          from_name=settings.ERP_NAME, site_url=settings.SYNAR_SITE_URL, source="environment")
    return None


def mail_enabled(company=None):
    return active_config(company) is not None


def send(company, subject, body, recipients, link="", attachments=(), prefix=True):
    """يرسل رسالة إن كان البريد مفعّلاً؛ يعيد True عند الإرسال. الفشل يُسجل ولا يوقف العملية.

    attachments: [(اسم الملف، المحتوى، النوع)]. prefix=False للرسائل الموجهة لعملاء المنشأة (دون اسم النظام).
    """
    config = active_config(company)
    recipients = sorted({r for r in recipients if r})
    if config is None or not recipients:
        return False
    if link and config.site_url:
        body = f"{body}\n\n{config.site_url.rstrip('/')}{link}"
    message = EmailMessage(f"[{settings.ERP_NAME}] {subject}" if prefix else subject, body, config.sender, recipients)
    for name, content, mimetype in attachments:
        message.attach(name, content, mimetype)

    def deliver():
        try:
            message.connection = config.connection()
            message.send()
        except Exception:
            log.exception("تعذر إرسال البريد")

    if getattr(settings, "SYNAR_EMAIL_ASYNC", True):
        # بعد حفظ العملية فقط، وفي خيط منفصل حتى لا ينتظر المستخدم خادم البريد
        transaction.on_commit(lambda: threading.Thread(target=deliver, daemon=True).start())
    else:
        deliver()
    return True


def send_test(obj, to):
    """يرسل رسالة اختبار بإعدادات السجل (ولو لم يُفعّل بعد) ويحفظ النتيجة. يعيد (نجاح، رسالة)."""
    if not obj.is_configured:
        return False, "أدخل خادم البريد وبريد المرسل أولاً."
    config = config_from(obj, "test")
    try:
        EmailMessage(f"[{settings.ERP_NAME}] رسالة اختبار",
                     f"هذه رسالة اختبار من {settings.ERP_NAME}. إعدادات البريد تعمل بشكل صحيح.",
                     config.sender, [to], connection=config.connection(timeout=15)).send()
        ok, message = True, f"تم إرسال رسالة الاختبار إلى {to}."
    except Exception as exc:  # نعرض سبب الفشل كما هو ليساعد في التصحيح
        ok, message = False, f"فشل الإرسال: {type(exc).__name__}: {exc}"[:500]
    obj.last_test_at, obj.last_test_ok, obj.last_test_message = timezone.now(), ok, message
    obj.save(update_fields=["last_test_at", "last_test_ok", "last_test_message"])
    return ok, message
