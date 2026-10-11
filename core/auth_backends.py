"""الدخول باسم المستخدم دون حساسية لحالة الأحرف (Admin = admin).

أسماء المستخدمين فريدة دون حساسية للحالة عند إنشائها (التسجيل وإضافة المستخدمين)، والجوالات تكبّر
الحرف الأول تلقائياً؛ فنقبل الاسم بأي حالة ما دام يطابق مستخدماً واحداً فقط.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class CaseInsensitiveModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is not None or not username or password is None:
            return user
        matches = list(get_user_model()._default_manager.filter(username__iexact=username.strip())[:2])
        if len(matches) == 1 and matches[0].check_password(password) and self.user_can_authenticate(matches[0]):
            return matches[0]
        return None
