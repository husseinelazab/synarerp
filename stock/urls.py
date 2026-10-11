from django.urls import path

from stock import reports

urlpatterns = [
    path("balance/", reports.stock_balance, name="stock_balance"),
    path("ledger/", reports.stock_ledger, name="stock_ledger"),
    path("gross-profit/", reports.gross_profit_report, name="gross_profit"),
    path("pricing/", reports.pricing_analysis, name="pricing_analysis"),
    path("price-history/", reports.price_history, name="price_history"),
]
