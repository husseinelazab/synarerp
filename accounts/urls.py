from django.urls import path

from accounts import reports

urlpatterns = [
    path("general-journal/", reports.general_journal, name="general_journal"),
    path("trial-balance/", reports.trial_balance, name="trial_balance"),
    path("general-ledger/", reports.general_ledger, name="general_ledger"),
    path("profit-and-loss/", reports.profit_and_loss, name="profit_and_loss"),
    path("balance-sheet/", reports.balance_sheet, name="balance_sheet"),
    path("vat/", reports.vat_report, name="vat_report"),
    path("chart-of-accounts/", reports.chart_of_accounts, name="chart_of_accounts"),
    path("balances/<str:party>/", reports.party_balances, name="party_balances"),
]
