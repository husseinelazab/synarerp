from django.urls import path

from accounts import bank, budget, cheques, opening_views, reports

urlpatterns = [
    path("bank-reconciliation/", bank.bank_reconciliation, name="bank_reconciliation"),
    path("opening/", opening_views.opening_wizard, name="opening_wizard"),
    path("general-journal/", reports.general_journal, name="general_journal"),
    path("trial-balance/", reports.trial_balance, name="trial_balance"),
    path("general-ledger/", reports.general_ledger, name="general_ledger"),
    path("profit-and-loss/", reports.profit_and_loss, name="profit_and_loss"),
    path("cash-flow/", reports.cash_flow, name="cash_flow"),
    path("balance-sheet/", reports.balance_sheet, name="balance_sheet"),
    path("vat/", reports.vat_report, name="vat_report"),
    path("cheques/", cheques.cheques_report, name="cheques_report"),
    path("cheques/<int:pk>/action/", cheques.cheque_action, name="cheque_action"),
    path("budget-variance/", budget.budget_variance, name="budget_variance"),
    path("foreign-balances/", reports.foreign_currency_balances, name="foreign_currency_balances"),
    path("chart-of-accounts/", reports.chart_of_accounts, name="chart_of_accounts"),
    path("balances/<str:party>/", reports.party_balances, name="party_balances"),
    path("aging/<str:party>/", reports.aging_report, name="aging_report"),
]
