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
    path("receipt/<int:pk>/", views.receipt, name="pos_receipt"),
]
