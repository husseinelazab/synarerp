from django import forms
from django.utils import timezone

from core.forms import style_form
from programs.models import (SpecialtyApplication, QuranCircle, QuranStudent, NewMuslim,
                             UmrahTrip, HealthPatient)
from website.forms import HoneypotMixin, clean_phone


class SpecialtyRegistrationForm(HoneypotMixin, forms.ModelForm):
    consent = forms.BooleanField(label="أوافق على معالجة بياناتي لغرض دراسة الطلب والتواصل معي وفق سياسة الخصوصية")

    class Meta:
        model = SpecialtyApplication
        fields = ["full_name", "phone", "email", "circle", "trip", "nationality", "language",
                  "birth_date", "service_needed", "preferred_date", "notes"]
        widgets = {"birth_date": forms.DateInput(attrs={"type": "date"}),
                   "preferred_date": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 3, "maxlength": 2000})}

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        specialty = company.association_specialty
        relevant = {"quran": {"circle", "birth_date"}, "dawah": {"trip", "nationality", "language"},
                    "health": {"birth_date", "service_needed", "preferred_date"}}[specialty]
        for name in {"circle", "trip", "nationality", "language", "birth_date", "service_needed", "preferred_date"} - relevant:
            self.fields.pop(name)
        if "circle" in self.fields:
            self.fields["circle"].queryset = QuranCircle.objects.filter(company=company, disabled=False)
            self.fields["circle"].empty_label = "تختار الجمعية الحلقة المناسبة"
        if "trip" in self.fields:
            self.fields["trip"].queryset = UmrahTrip.objects.filter(company=company, status="open", departure_date__gte=timezone.localdate())
            self.fields["trip"].empty_label = "تسجيل في خدمات الدعوة دون رحلة"
        for name in ("language", "service_needed"):
            if name in self.fields:
                self.fields[name].required = True
        self.fields["notes"].max_length = 2000

    def clean_notes(self):
        value = self.cleaned_data.get("notes", "")
        if len(value) > 2000:
            raise forms.ValidationError("الحد الأقصى للملاحظات 2000 حرف.")
        return value

    def clean_phone(self):
        return clean_phone(self.cleaned_data["phone"])

    def clean_birth_date(self):
        value = self.cleaned_data.get("birth_date")
        if value and value > timezone.localdate():
            raise forms.ValidationError("تاريخ الميلاد لا يكون في المستقبل.")
        return value

    def clean_preferred_date(self):
        value = self.cleaned_data.get("preferred_date")
        if value and value < timezone.localdate():
            raise forms.ValidationError("اختر يوماً قادماً للخدمة.")
        return value


class SpecialtyReviewForm(forms.Form):
    decision = forms.ChoiceField(label="القرار", choices=[("", "— اختر القرار —"), ("accept", "قبول"), ("reject", "رفض")])
    review_note = forms.CharField(label="ملاحظات المراجعة / سبب الرفض", required=False, max_length=2000,
                                 widget=forms.Textarea(attrs={"rows": 3}))
    circle = forms.ModelChoiceField(label="الحلقة المعتمدة", queryset=QuranCircle.objects.none(), required=False)
    existing_profile = forms.ModelChoiceField(label="ربط بملف مسجل سابقاً (اختياري)", queryset=NewMuslim.objects.none(), required=False,
                                              help_text="اتركه فارغاً لإنشاء ملف جديد. تحقق من الاسم والجوال قبل الربط.")
    appointment_at = forms.DateTimeField(label="الموعد الصحي المعتمد (اختياري)", required=False,
                                         widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    clinician = forms.CharField(label="الطبيب / الممارس", max_length=150, required=False)
    clinic = forms.CharField(label="العيادة", max_length=150, required=False)

    def __init__(self, *args, application, allow_existing_profile=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application
        company = application.company
        specialty = application.specialty
        profiles = {"quran": QuranStudent, "dawah": NewMuslim, "health": HealthPatient}
        self.fields["existing_profile"].queryset = profiles[specialty].objects.filter(company=company).order_by("full_name")
        if specialty == "quran":
            self.fields["existing_profile"].queryset = self.fields["existing_profile"].queryset.filter(disabled=False)
            self.fields["circle"].queryset = QuranCircle.objects.filter(company=company, disabled=False)
            self.fields["circle"].initial = application.circle_id
        else:
            self.fields.pop("circle")
        if specialty != "health":
            for name in ("appointment_at", "clinician", "clinic"):
                self.fields.pop(name)
        if not allow_existing_profile:
            self.fields["existing_profile"].queryset = self.fields["existing_profile"].queryset.none()
        style_form(self)

    def clean(self):
        data = super().clean()
        if data.get("decision") == "reject":
            if not data.get("review_note"):
                self.add_error("review_note", "اذكر سبب الرفض لتوثيق القرار.")
            return data
        if self.application.specialty == "quran" and not data.get("circle") and not data.get("existing_profile"):
            self.add_error("circle", "اختر الحلقة قبل قبول الطالب.")
        if data.get("appointment_at"):
            if data["appointment_at"] <= timezone.now():
                self.add_error("appointment_at", "الموعد يجب أن يكون في المستقبل.")
            for name in ("clinician", "clinic"):
                if not data.get(name):
                    self.add_error(name, "هذا الحقل مطلوب لتحديد الموعد.")
        return data


class HealthSchedulingForm(forms.ModelForm):
    class Meta:
        from programs.models import HealthAppointment
        model = HealthAppointment
        fields = ["scheduled_at", "clinician", "clinic", "notes"]
        widgets = {"scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
                   "notes": forms.Textarea(attrs={"rows": 2, "maxlength": 2000})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)

    def clean_scheduled_at(self):
        value = self.cleaned_data["scheduled_at"]
        if value <= timezone.now():
            raise forms.ValidationError("الموعد يجب أن يكون في المستقبل.")
        return value
