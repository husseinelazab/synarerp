"""التحقق الثنائي: التفعيل والإلغاء، وخطوة الرمز بعد كلمة المرور، والحد من محاولات الدخول الخاطئة."""
import io

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from core import totp
from core.models import UserTwoFactor

MAX_FAILURES = 5          # محاولات كلمة مرور خاطئة قبل الإيقاف المؤقت
LOCK_SECONDS = 15 * 60
MAX_CODE_ATTEMPTS = 5     # محاولات رمز خاطئة قبل العودة لصفحة الدخول
SESSION_USER, SESSION_BACKEND, SESSION_NEXT, SESSION_TRIES = "2fa_user", "2fa_backend", "2fa_next", "2fa_tries"


def _qr_svg(data):
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")


def _lock_keys(request, username):
    return [f"login-fail:user:{(username or '').strip().lower()}", f"login-fail:ip:{_client_ip(request)}"]


def is_locked(request, username):
    return any((cache.get(key) or 0) >= MAX_FAILURES * (4 if ":ip:" in key else 1) for key in _lock_keys(request, username))


def record_failure(request, username):
    for key in _lock_keys(request, username):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, LOCK_SECONDS)


def clear_failures(request, username):
    cache.delete_many(_lock_keys(request, username))


class LoginView(auth_views.LoginView):
    """الدخول بكلمة المرور، ثم برمز تطبيق المصادقة إن كان مفعّلاً."""

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if is_locked(request, username):
            form = self.get_form()
            form.errors.clear()
            return self.render_to_response(self.get_context_data(
                form=form, locked=True, lock_minutes=LOCK_SECONDS // 60))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        record_failure(self.request, self.request.POST.get("username", ""))
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        clear_failures(self.request, user.username)
        if UserTwoFactor.objects.filter(user=user, enabled=True).exists():
            self.request.session[SESSION_USER] = user.pk
            self.request.session[SESSION_BACKEND] = user.backend
            self.request.session[SESSION_NEXT] = self.get_redirect_url()
            self.request.session[SESSION_TRIES] = 0
            return redirect("login_verify")
        return super().form_valid(form)


def verify_login(request):
    """الخطوة الثانية: رمز التطبيق أو رمز استرداد."""
    user_id = request.session.get(SESSION_USER)
    user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    record = UserTwoFactor.objects.filter(user=user, enabled=True).first() if user else None
    if record is None:
        return redirect("login")
    error = ""
    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        step = totp.verify(totp.decrypt_secret(record.secret_encrypted), code, record.last_step)
        used_recovery = False
        if step is None and totp.hash_code(code) in record.recovery_codes:
            record.recovery_codes.remove(totp.hash_code(code))
            used_recovery = True
        if step is not None or used_recovery:
            if step is not None:
                record.last_step = step
            record.save(update_fields=["last_step", "recovery_codes"])
            next_url = request.session.get(SESSION_NEXT) or settings.LOGIN_REDIRECT_URL
            backend = request.session.get(SESSION_BACKEND)
            for key in (SESSION_USER, SESSION_BACKEND, SESSION_NEXT, SESSION_TRIES):
                request.session.pop(key, None)
            auth_login(request, user, backend=backend)
            if used_recovery:
                messages.warning(request, f"دخلت برمز استرداد؛ تبقى لك {len(record.recovery_codes)} رمزاً. "
                                          "أنشئ رموزاً جديدة من «حسابي ← التحقق الثنائي» إن قاربت على النفاد.")
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = settings.LOGIN_REDIRECT_URL
            return redirect(next_url)
        tries = request.session.get(SESSION_TRIES, 0) + 1
        request.session[SESSION_TRIES] = tries
        if tries >= MAX_CODE_ATTEMPTS:
            for key in (SESSION_USER, SESSION_BACKEND, SESSION_NEXT, SESSION_TRIES):
                request.session.pop(key, None)
            record_failure(request, user.username)
            messages.error(request, "تجاوزت عدد المحاولات. سجّل الدخول من جديد.")
            return redirect("login")
        error = "الرمز غير صحيح أو انتهت صلاحيته. أدخل الرمز الحالي من التطبيق."
    return render(request, "registration/login_verify.html", {"error": error})


@login_required
def two_factor_setup(request):
    record = UserTwoFactor.objects.filter(user=request.user).first()
    enabled = bool(record and record.enabled)
    recovery = request.session.pop("2fa_new_codes", None)
    if request.method == "POST":
        action = request.POST.get("action")
        code = request.POST.get("code", "")
        if action == "enable" and not enabled:
            secret = request.session.get("2fa_pending_secret")
            step = totp.verify(secret, code) if secret else None
            if step is None:
                messages.error(request, "الرمز غير صحيح. تأكد من ضبط وقت الجوال تلقائياً وأدخل الرمز الحالي.")
                return redirect("two_factor_setup")
            codes, hashes = totp.new_recovery_codes()
            UserTwoFactor.objects.update_or_create(user=request.user, defaults={
                "secret_encrypted": totp.encrypt_secret(secret), "enabled": True, "recovery_codes": hashes, "last_step": step,
                "enabled_at": timezone.now()})
            request.session.pop("2fa_pending_secret", None)
            request.session["2fa_new_codes"] = codes
            messages.success(request, "تم تفعيل التحقق الثنائي. احفظ رموز الاسترداد في مكان آمن.")
            return redirect("two_factor_setup")
        if action in ("disable", "regenerate") and enabled:
            if not request.user.check_password(request.POST.get("password", "")) or \
                    totp.verify(totp.decrypt_secret(record.secret_encrypted), code, record.last_step) is None:
                messages.error(request, "كلمة المرور أو الرمز غير صحيح.")
                return redirect("two_factor_setup")
            if action == "disable":
                if getattr(request, "company", None) and request.company.require_two_factor:
                    messages.error(request, "شركتك تشترط التحقق الثنائي؛ لا يمكن إلغاؤه.")
                    return redirect("two_factor_setup")
                record.delete()
                messages.success(request, "تم إلغاء التحقق الثنائي.")
            else:
                codes, record.recovery_codes = totp.new_recovery_codes()
                record.save(update_fields=["recovery_codes"])
                request.session["2fa_new_codes"] = codes
                messages.success(request, "تم إنشاء رموز استرداد جديدة؛ الرموز القديمة لم تعد صالحة.")
            return redirect("two_factor_setup")
    context = {"enabled": enabled, "record": record, "recovery": recovery}
    if not enabled:
        secret = request.session.get("2fa_pending_secret") or totp.generate_secret()
        request.session["2fa_pending_secret"] = secret
        uri = totp.provisioning_uri(secret, request.user.username, settings.ERP_NAME_EN)
        context.update({"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "qr": _qr_svg(uri)})
    return render(request, "registration/two_factor.html", context)
