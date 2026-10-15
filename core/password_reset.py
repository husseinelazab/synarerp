"""استعادة كلمة المرور بالبريد — متاحة فقط إذا فُعّل خادم بريد (للمنصة أو لشركة المستخدم)."""
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.core.mail import EmailMultiAlternatives
from django.shortcuts import render
from django.template import loader

from core.mail import active_config


def reset_available():
    from core.models import EmailSettings

    return active_config(None) is not None or EmailSettings.objects.filter(
        company__isnull=False, enabled=True).exclude(host="").exists()


def _config_for(user):
    membership = user.memberships.filter(is_active=True).select_related("company").order_by("id").first()
    return active_config(membership.company if membership else None)


class SynarPasswordResetForm(PasswordResetForm):
    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email,
                  html_email_template_name=None):
        config = _config_for(context["user"])
        if config is None:
            return  # لا خادم بريد لهذا المستخدم؛ لا نكشف ذلك للطالب
        # رابط النظام من الإعدادات إن وُجد (أدق خلف الوكيل العكسي)، وإلا من عنوان الطلب
        context = {**context, "base_url": config.site_url.rstrip("/") or f"{context['protocol']}://{context['domain']}"}
        subject = "".join(loader.render_to_string(subject_template_name, context).splitlines())
        body = loader.render_to_string(email_template_name, context)
        EmailMultiAlternatives(subject, body, config.sender, [to_email], connection=config.connection()).send(
            fail_silently=True)


class ResetView(auth_views.PasswordResetView):
    form_class = SynarPasswordResetForm
    template_name = "registration/reset_form.html"
    email_template_name = "registration/reset_email.txt"
    subject_template_name = "registration/reset_subject.txt"
    success_url = "/password-reset/sent/"

    def dispatch(self, request, *args, **kwargs):
        if not reset_available():
            return render(request, "registration/reset_unavailable.html")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        from django.shortcuts import redirect

        from website.ratelimit import too_many

        # حد للطلبات من الجهاز نفسه حتى لا يُستغل لإغراق بريد أحد؛ الرد نفسه حتى لا يُعرف أن الطلب أُوقف
        if too_many(self.request, "password_reset", 5):
            from core.security_log import event

            event("password_reset_locked", self.request)
            return redirect(self.success_url)
        return super().form_valid(form)


sent = auth_views.PasswordResetDoneView.as_view(template_name="registration/reset_sent.html")
confirm = auth_views.PasswordResetConfirmView.as_view(template_name="registration/reset_confirm.html",
                                                       success_url="/password-reset/complete/")
complete = auth_views.PasswordResetCompleteView.as_view(template_name="registration/reset_complete.html")
