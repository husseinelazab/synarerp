"""نموذج طلب السيارة: حقلا الوقت بإدخال تاريخ وساعة (النماذج العامة تعرض التاريخ فقط)."""
from django import forms

from core.forms import formfield_callback
from fleet.models import VehicleRequest

DATETIME_WIDGET = {"type": "datetime-local"}


class VehicleRequestForm(forms.ModelForm):
    class Meta:
        model = VehicleRequest
        fields = ["purpose", "destination", "start_at", "end_at", "passengers", "vehicle", "start_odometer",
                  "posting_date", "remarks"]
        formfield_callback = formfield_callback
        widgets = {
            "start_at": forms.DateTimeInput(attrs=DATETIME_WIDGET, format="%Y-%m-%dT%H:%M"),
            "end_at": forms.DateTimeInput(attrs=DATETIME_WIDGET, format="%Y-%m-%dT%H:%M"),
        }
