from django.urls import path

from accounts import bank, opening_views, reconcile, reports, statements

urlpatterns = [
    path("bank-reconciliation/", bank.bank_reconciliation, name="bank_reconciliation"),
    path("reconcile/", reconcile.reconcile_tool, name="payment_reconcile"),
    path("opening/", opening_views.opening_wizard, name="opening_wizard"),
    path("general-journal/", reports.general_journal, name="general_journal"),
    path("trial-balance/", reports.trial_balance, name="trial_balance"),
    path("general-ledger/", reports.general_ledger, name="general_ledger"),
    path("profit-and-loss/", reports.profit_and_loss, name="profit_and_loss"),
    path("balance-sheet/", reports.balance_sheet, name="balance_sheet"),
    path("vat/", reports.vat_report, name="vat_report"),
    path("cash-flow/", statements.cash_flow, name="cash_flow"),
    path("profit-and-loss/compare/", statements.comparative_pnl, name="comparative_pnl"),
    path("balance-sheet/compare/", statements.comparative_balance_sheet, name="comparative_balance_sheet"),
    path("chart-of-accounts/", reports.chart_of_accounts, name="chart_of_accounts"),
    path("balances/<str:party>/", reports.party_balances, name="party_balances"),
    path("aging/<str:party>/", reports.aging_report, name="aging_report"),
]
