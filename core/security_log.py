"""سجل أحداث الأمان (الدخول الناجح والفاشل، والإيقاف المؤقت، والتحقق الثنائي) في سجل الخادم.

لا يُسجَّل أي سر: لا كلمة مرور ولا رمز، فقط اسم المستخدم المُدخل وعنوان الجهاز.
"""
import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from core.utils import client_ip

log = logging.getLogger("synar.security")


def event(name, request=None, username=""):
    # المحاولات الفاشلة والإيقاف تحذيرات (تظهر في السجل بأي مستوى)، والباقي معلومات
    level = logging.WARNING if name.endswith(("failed", "locked")) else logging.INFO
    log.log(level, "%s user=%s ip=%s", name, (username or "")[:150], client_ip(request) if request else "")


@receiver(user_logged_in, dispatch_uid="synar-security-login")
def _logged_in(sender, request, user, **kwargs):
    event("login", request, user.get_username())


@receiver(user_login_failed, dispatch_uid="synar-security-login-failed")
def _login_failed(sender, credentials, request=None, **kwargs):
    event("login_failed", request, credentials.get("username", ""))


@receiver(user_logged_out, dispatch_uid="synar-security-logout")
def _logged_out(sender, request, user, **kwargs):
    if user is not None:
        event("logout", request, user.get_username())
