from django.urls import path

from stock import reports

urlpatterns = [
    path("balance/", reports.stock_balance, name="stock_balance"),
    path("ledger/", reports.stock_ledger, name="stock_ledger"),
]
