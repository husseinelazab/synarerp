"""Public login names belong to memberships; Django's internal user key remains globally unique."""
import uuid
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from core.models import Company, Membership


@transaction.atomic
def create_company_user(company, username, email, password, **extra):
    Company.objects.select_for_update().get(pk=company.pk)
    username = username.strip()
    if Membership.objects.filter(company=company, login_key=username.casefold()).exists():
        raise ValidationError("اسم المستخدم مستخدم داخل هذه الجهة.")
    User = get_user_model()
    internal = username
    if User.objects.filter(username__iexact=internal).exists():
        internal = f"{username[:110]}@{company.login_code}-{uuid.uuid4().hex[:8]}"
    try:
        with transaction.atomic():
            return User.objects.create_user(internal, email, password, **extra)
    except IntegrityError:
        # Two different organizations may register the same alias concurrently.
        if not User.objects.filter(username__iexact=internal).exists():
            raise
        internal = f"{username[:110]}@{company.login_code}-{uuid.uuid4().hex[:8]}"
        return User.objects.create_user(internal, email, password, **extra)
