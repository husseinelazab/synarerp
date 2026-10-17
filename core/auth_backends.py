"""دخول بأسماء مستخدمين خاصة بكل جهة، مع دعم الدخول القديم للأسماء غير المكررة."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class CaseInsensitiveModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        from core.models import Membership
        if not username or password is None:
            return None
        code = (request.POST.get("company_code", "") if request is not None else kwargs.get("company_code", "")).strip().upper()
        User = get_user_model()
        if code:
            member = Membership.objects.filter(company__login_code=code, login_key=username.strip().casefold(),
                                                is_active=True).select_related("user").first()
            user = member.user if member else None
            if user and user.check_password(password) and self.user_can_authenticate(user):
                user.login_company_id = member.company_id
                if request is not None and hasattr(request, "session"):
                    request.session["company_id"] = member.company_id
                return user
            if user is None:
                User().set_password(password)
            return None
        # Preserve platform and unambiguous legacy accounts; require the code for duplicate public aliases.
        candidates = list(User.objects.filter(username__iexact=username.strip())[:2])
        user = candidates[0] if len(candidates) == 1 else None
        if user and user.is_superuser:
            return user if user.check_password(password) and self.user_can_authenticate(user) else None
        aliases = list(Membership.objects.filter(login_key=username.strip().casefold(), is_active=True)
                       .values_list("user_id", flat=True).distinct()[:2])
        if len(aliases) > 1:
            return None
        if aliases:
            user = User.objects.filter(pk=aliases[0]).first()
        return user if user and user.check_password(password) and self.user_can_authenticate(user) else None
