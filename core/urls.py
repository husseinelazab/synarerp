from django.urls import path

from core import import_views, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("module/<str:module>/", views.module_page, name="module"),
    path("account/notifications/", views.notifications, name="notifications"),
    path("account/notifications/<int:pk>/", views.notification_open, name="notification_open"),
    path("api/item/<int:pk>/", views.item_api, name="item_api"),
    path("app/<slug:slug>/", views.doc_list, name="doc_list"),
    path("app/<slug:slug>/new/", views.doc_form, name="doc_new"),
    path("app/<slug:slug>/import/", import_views.import_page, name="doc_import"),
    path("app/<slug:slug>/import/<int:pk>/", import_views.import_job, name="doc_import_job"),
    path("app/<slug:slug>/template.<str:fmt>", import_views.import_template, name="doc_import_template"),
    path("app/<slug:slug>/export/", import_views.export_list, name="doc_export"),
    path("app/<slug:slug>/<int:pk>/", views.doc_detail, name="doc_detail"),
    path("app/<slug:slug>/<int:pk>/edit/", views.doc_form, name="doc_edit"),
    path("app/<slug:slug>/<int:pk>/print/", views.doc_print, name="doc_print"),
    path("app/<slug:slug>/<int:pk>/<str:action>/", views.doc_action, name="doc_action"),
]
