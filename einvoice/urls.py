from django.urls import path

from einvoice import views

urlpatterns = [
    path("", views.einvoice_list, name="einvoice_list"),
    path("settings/", views.settings_page, name="einvoice_settings"),
    path("resubmit/", views.resubmit_all, name="einvoice_resubmit_all"),
    path("<int:pk>/resubmit/", views.resubmit, name="einvoice_resubmit"),
    path("<int:pk>/xml/", views.download_xml, name="einvoice_xml"),
]
