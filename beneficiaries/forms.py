from django import forms
from django.contrib.auth import get_user_model

from beneficiaries.models import AidCommittee
from core.forms import formfield_callback


class AidCommitteeForm(forms.ModelForm):
    class Meta:
        model = AidCommittee
        fields = ["name", "is_active", "members", "quorum", "decision_rule", "review_assessments",
                  "disbursement_threshold"]
        widgets = {"members": forms.CheckboxSelectMultiple}
        formfield_callback = formfield_callback

    def __init__(self, *args, **kwargs):
        from core.models import Company

        super().__init__(*args, **kwargs)
        company = Company.current()
        # الأعضاء من مستخدمي الجمعية (دون حسابات الكاشير وبوابة الموظف)
        self.fields["members"].queryset = get_user_model().objects.filter(
            memberships__company=company, memberships__is_active=True,
            memberships__role__in=["owner", "admin", "user"]).distinct().order_by("first_name", "username")
        self.fields["members"].label_from_instance = lambda u: u.get_full_name() or u.username

    def clean(self):
        data = super().clean()
        members, quorum = data.get("members"), data.get("quorum")
        if members is not None and quorum and quorum > len(members):
            self.add_error("quorum", f"النصاب ({quorum}) أكبر من عدد الأعضاء المختارين ({len(members)}).")
        return data
