from django import forms

from core.forms import PrintSettingsForm, style_form
from website.models import Website


class WebsiteForm(forms.ModelForm):
    hero_file = forms.FileField(label="صورة الواجهة", required=False,
                                help_text="PNG أو JPG أو WebP بحجم 800 كيلوبايت كحد أقصى، يُفضّل بعرض 1600 بكسل")
    remove_hero = forms.BooleanField(label="حذف صورة الواجهة", required=False)
    MAX_IMAGE = 800 * 1024

    class Meta:
        model = Website
        fields = ["slug", "domain", "is_published", "tagline", "hero_title", "hero_subtitle", "about", "primary_color",
                  "fundraising_license_number", "fundraising_license_expiry", "bank_accounts", "whatsapp",
                  "working_hours", "map_url", "x_url", "instagram_url", "snapchat_url", "youtube_url", "linkedin_url"]
        widgets = {"primary_color": forms.TextInput(attrs={"type": "color", "style": "height:44px;width:120px;padding:4px"}),
                   "about": forms.Textarea(attrs={"rows": 4}), "bank_accounts": forms.Textarea(attrs={"rows": 3}),
                   "fundraising_license_expiry": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.hero_image:
            del self.fields["remove_hero"]
        style_form(self)

    def clean_hero_file(self):
        upload = self.cleaned_data.get("hero_file")
        if not upload:
            return None
        if upload.size > self.MAX_IMAGE:
            raise forms.ValidationError("حجم الصورة أكبر من 800 كيلوبايت.")
        import base64

        data = upload.read()
        kind = next((t for magic, t in PrintSettingsForm.LOGO_TYPES.items() if data.startswith(magic)), None)
        if kind is None:
            raise forms.ValidationError("الصيغ المسموحة: PNG أو JPG أو WebP.")
        return f"data:{kind};base64,{base64.b64encode(data).decode()}"

    def save(self, commit=True):
        site = super().save(commit=False)
        if self.cleaned_data.get("remove_hero"):
            site.hero_image = ""
        if self.cleaned_data.get("hero_file"):
            site.hero_image = self.cleaned_data["hero_file"]
        if commit:
            site.save()
        return site
