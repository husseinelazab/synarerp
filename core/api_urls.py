from django.urls import path

from core import api

urlpatterns = [
    path("", api.index, name="api_index"),
    path("reports/stock-balance/", api.stock_balance, name="api_stock_balance"),
    path("reports/account-balances/", api.account_balances, name="api_account_balances"),
    path("scales/", api.scales_sync, name="api_scales"),
    path("scales/<int:pk>/report/", api.scale_report, name="api_scale_report"),
    path("<slug:slug>/", api.collection, name="api_collection"),
    path("<slug:slug>/<int:pk>/", api.item, name="api_item"),
    path("<slug:slug>/<int:pk>/<str:name>/", api.action, name="api_action"),
]
