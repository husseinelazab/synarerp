from accounts.models import (Account, BankTransaction, CostCenter, JournalEntry, JournalEntryAccount, OpeningAccountBalance, OpeningAccountLine,
                             OpeningPartyBalance, OpeningPartyLine, PaymentEntry, PeriodClosingVoucher)
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="journal-entry", model=JournalEntry, module="accounts",
    list_fields=["name", "posting_date", "entry_type", "total_debit", "remarks"],
    form_fields=["posting_date", "entry_type", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(JournalEntryAccount, ["account", "customer", "supplier", "debit", "credit", "cost_center", "description"],
                     label="الحسابات"),
    totals=["total_debit", "total_credit"],
))
register(DocType(
    slug="payment-entry", model=PaymentEntry, module="accounts",
    list_fields=["name", "posting_date", "payment_type", "customer", "supplier", "amount"],
    form_fields=["payment_type", "posting_date", "customer", "supplier", "account", "amount", "currency", "exchange_rate",
                 "sales_invoice", "purchase_invoice", "reference_no", "remarks"],
    search_fields=["name", "customer__name", "supplier__name", "reference_no"],
))
register(DocType(
    slug="account", model=Account, module="accounts",
    list_fields=["number", "name", "parent", "root_type", "account_type", "is_group"],
    form_fields=["number", "name", "parent", "is_group", "root_type", "account_type", "disabled"],
    search_fields=["number", "name"],
))
register(DocType(
    slug="opening-account", model=OpeningAccountBalance, module="accounts",
    list_fields=["name", "posting_date", "remarks"], form_fields=["posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(OpeningAccountLine, ["account", "debit", "credit", "description"], label="الحسابات"),
))
register(DocType(
    slug="opening-party", model=OpeningPartyBalance, module="accounts",
    list_fields=["name", "posting_date", "party_type", "remarks"], form_fields=["party_type", "posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(OpeningPartyLine, ["customer", "supplier", "amount", "reference"], label="الأطراف"),
))
register(DocType(
    slug="period-closing", model=PeriodClosingVoucher, module="accounts",
    list_fields=["name", "fiscal_year", "posting_date", "net_profit"], form_fields=["fiscal_year", "closing_account", "remarks"],
    search_fields=["name"], totals=["net_profit"],
))
register(DocType(
    slug="cost-center", model=CostCenter, module="accounts",
    list_fields=["name", "parent", "is_group", "disabled"], search_fields=["name"],
))
register(DocType(
    slug="bank-transaction", model=BankTransaction, module="accounts",
    list_fields=["date", "account", "description", "reference", "deposit", "withdrawal"],
    search_fields=["description", "reference"],
))
