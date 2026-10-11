"""عزل بيانات الشركات (Multi-tenancy) بنظام الصفوف في قاعدة بيانات مشتركة.

كل نموذج يرث TenantModel يحمل حقل company، ومديره الافتراضي يُرشّح الاستعلامات تلقائياً
بالشركة الحالية للطلب (تُضبط في TenantMiddleware). بذلك تُعزل القوائم والتقارير والقوائم المنسدلة
في النماذج دون الحاجة لتذكر الترشيح في كل استعلام.
"""
import contextvars
from contextlib import contextmanager

from django.db import models

_current_company = contextvars.ContextVar("current_company", default=None)
_current_user = contextvars.ContextVar("current_user", default=None)


_current_ip = contextvars.ContextVar("current_ip", default="")


def get_current_ip():
    return _current_ip.get()


def get_current_user():
    """المستخدم صاحب الطلب الحالي (لسجلات التدقيق مثل سجل تغيّر الأسعار)."""
    return _current_user.get()


def set_current_user(user):
    return _current_user.set(user)


def get_current_company():
    return _current_company.get()


def set_current_company(company):
    return _current_company.set(company)


def reset_current_company(token):
    _current_company.reset(token)


@contextmanager
def tenant_context(company):
    token = _current_company.set(company)
    try:
        yield company
    finally:
        _current_company.reset(token)


class TenantManager(models.Manager):
    def get_queryset(self):
        queryset = super().get_queryset()
        company = get_current_company()
        return queryset.filter(company=company) if company is not None else queryset


def tenant_unique(*fields, model_label):
    """قيد تفرد داخل الشركة الواحدة مع رسالة عربية."""
    return models.UniqueConstraint(
        fields=["company", *fields],
        name=f"{model_label}_{'_'.join(fields)}_per_company",
        violation_error_message="يوجد سجل آخر بنفس القيمة في هذه الشركة.",
    )
