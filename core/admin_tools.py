from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from core.forms import style_form
from core.permissions import company_modules


class ModuleLabelsForm(forms.Form):
    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        for key, label in company_modules(company) + [("settings", "الإعدادات")]:
            self.fields[key] = forms.CharField(label=label, max_length=60, required=False,
                initial=company.module_labels.get(key, label))
        style_form(self)


@login_required
def module_labels(request):
    if not request.membership.can_manage:
        raise PermissionDenied
    form = ModuleLabelsForm(request.POST if request.method == "POST" else None, company=request.company)
    if request.method == "POST" and form.is_valid():
        request.company.module_labels = {key:value.strip() for key,value in form.cleaned_data.items() if value.strip()}
        request.company.save(update_fields=["module_labels"])
        messages.success(request,"تم تحديث مسميات الوحدات لهذه الجهة.")
        return redirect("module_labels")
    return render(request,"settings/module_labels.html",{"form":form})
