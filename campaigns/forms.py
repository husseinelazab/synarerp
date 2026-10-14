from django import forms
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from campaigns.models import Campaign, CampaignSettings
from core.forms import formfield_callback, style_form

# نفس قاعدة campaigns.service.sms_segments لكن في المتصفح أثناء الكتابة
COUNTER_JS = """<script>(function(){
var GSM="@£$¥èéùìòÇ\\nØø\\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà",EXT="^{}\\\\[~]|€";
function count(t){var g=true,n=0;for(var i=0;i<t.length;i++){var c=t[i];if(EXT.indexOf(c)>=0){n+=2}else if(GSM.indexOf(c)>=0){n+=1}else{g=false;break}}
 if(!g){n=t.length}var s=g?160:70,m=g?153:67;return{n:n,seg:n?(n<=s?1:Math.ceil(n/m)):0,enc:g?"GSM-7":"UCS-2",s:s,m:m}}
document.querySelectorAll("textarea[data-sms-counter]").forEach(function(el){var out=document.getElementById(el.id+"_counter");
 function upd(){var r=count(el.value);out.textContent=r.n+" حرفاً — "+r.seg+" جزء SMS ("+r.enc+": "+r.s+" حرفاً للرسالة الواحدة، "+r.m+" للمتعددة). المتغيرات تُحسب بطولها بعد التعويض."}
 el.addEventListener("input",upd);upd();});})();</script>"""


class SmsCounterTextarea(forms.Textarea):
    def render(self, name, value, attrs=None, renderer=None):
        attrs = {**(attrs or {}), "data-sms-counter": "1"}
        html = super().render(name, value, attrs, renderer)
        element_id = (attrs or {}).get("id") or f"id_{name}"
        return format_html('{}<div class="muted small" id="{}_counter"></div>{}', html, element_id,
                           mark_safe(COUNTER_JS))  # noqa: S308 — نص ثابت


class CampaignForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ["title", "channel", "segment", "message", "link", "opportunity", "email_subject", "scheduled_at"]
        formfield_callback = formfield_callback

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # formfield_callback يفرض عناصر التاريخ والنص العامة، فنضبط عناصرنا هنا
        self.fields["message"].widget = SmsCounterTextarea(attrs={"rows": 5})
        self.fields["scheduled_at"].widget = forms.DateTimeInput(attrs={"type": "datetime-local"},
                                                                 format="%Y-%m-%dT%H:%M")
        self.fields["scheduled_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]
        if not self.instance.pk and not self.initial.get("message"):
            self.initial["message"] = "{الاسم}، "


class CampaignSettingsForm(forms.ModelForm):
    class Meta:
        model = CampaignSettings
        fields = ["sms_optout_footer", "due_reminder_enabled", "due_days_before", "due_text",
                  "overdue_reminder_enabled", "overdue_text"]
        widgets = {"due_text": SmsCounterTextarea(attrs={"rows": 3}),
                   "overdue_text": SmsCounterTextarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)
