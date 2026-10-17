from django.urls import path

from pos import views

urlpatterns = [
    path("", views.terminal, name="pos_terminal"),
    path("open/", views.open_shift, name="pos_open_shift"),
    path("close/", views.close_shift, name="pos_close_shift"),
    path("shifts/", views.shift_list, name="pos_shifts"),
    path("shifts/<int:pk>/", views.shift_report, name="pos_shift_report"),
    path("api/items/", views.items_api, name="pos_items"),
    path("api/checkout/", views.checkout_api, name="pos_checkout"),
    path("api/quote/", views.quote_api, name="pos_quote"),
    path("api/sync/", views.sync_api, name="pos_sync"),
    path("sw.js", views.service_worker, name="pos_sw"),
    path("api/invoices/", views.invoice_search_api, name="pos_invoice_search"),
    path("api/invoice/", views.invoice_api, name="pos_invoice"),
    path("api/return/", views.return_api, name="pos_return"),
    path("receipt/<int:pk>/", views.receipt, name="pos_receipt"),
]
