from django.urls import path

from buying import ai_views

urlpatterns = [
    path("buying/ai-invoice/", ai_views.ai_invoice_upload, name="ai_invoice_upload"),
    path("buying/ai-invoice/review/", ai_views.ai_invoice_review, name="ai_invoice_review"),
    path("settings/ai/", ai_views.ai_settings, name="ai_settings"),
]
