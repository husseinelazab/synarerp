from django import forms

from accounts.models import Account
from core.forms import formfield_callback


def next_child_number(parent):
    """الرقم التالي تحت الحساب الأب: 11 ← 1117، 1101 ← 110101 (رقمان لكل مستوى)."""
    if parent is None or not parent.number:
        return ""
    numbers = [c.number for c in Account.objects.filter(parent=parent) if c.number and c.number.isdigit()
               and c.number.startswith(parent.number)]
    if numbers:
        return str(max(int(n) for n in numbers) + 1)
    return parent.number + "01"


class AccountForm(forms.ModelForm):
    """حساب جديد أو تعديل: يُختار الأب من الحسابات الرئيسية فقط، ونوعه الجذري يُؤخذ من الأب تلقائياً."""

    class Meta:
        model = Account
        fields = ["parent", "number", "name", "is_group", "root_type", "account_type", "cash_flow_category",
                  "zakat_treatment", "disabled"]
        formfield_callback = formfield_callback

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = Account.objects.filter(is_group=True).order_by("number", "name")
        self.fields["parent"].label_from_instance = lambda a: f"{a.number} - {a.name}" if a.number else a.name
        self.fields["parent"].help_text = "الحساب الرئيسي الذي يندرج تحته. فارغ = حساب جذري (أصول، خصوم…)"
        self.fields["root_type"].required = False
        self.fields["root_type"].help_text = "يُحدد تلقائياً من الحساب الأب"
        parent_id = self.data.get("parent") or self.initial.get("parent")
        if not self.instance.pk and parent_id and not self.data:
            parent = Account.objects.filter(pk=parent_id).first()
            if parent is not None:
                self.initial.setdefault("number", next_child_number(parent))
                self.initial.setdefault("root_type", parent.root_type)

    def clean(self):
        data = super().clean()
        parent = data.get("parent")
        if parent is not None:
            data["root_type"] = parent.root_type
            self.instance.root_type = parent.root_type
        elif not data.get("root_type"):
            self.add_error("root_type", "حدد النوع الجذري للحساب الجذري.")
        number = (data.get("number") or "").strip()
        if number and Account.objects.filter(number=number).exclude(pk=self.instance.pk).exists():
            self.add_error("number", "رقم الحساب مستخدم لحساب آخر.")
        return data
