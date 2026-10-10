from accounts.models import Account, JournalEntry, JournalEntryAccount, PaymentEntry
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="journal-entry", model=JournalEntry, module="accounts",
    list_fields=["name", "posting_date", "entry_type", "total_debit", "remarks"],
    form_fields=["posting_date", "entry_type", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(JournalEntryAccount, ["account", "customer", "supplier", "debit", "credit", "description"], label="الحسابات"),
    totals=["total_debit", "total_credit"],
))
register(DocType(
    slug="payment-entry", model=PaymentEntry, module="accounts",
    list_fields=["name", "posting_date", "payment_type", "customer", "supplier", "amount"],
    form_fields=["payment_type", "posting_date", "customer", "supplier", "account", "amount",
                 "sales_invoice", "purchase_invoice", "reference_no", "remarks"],
    search_fields=["name", "customer__name", "supplier__name", "reference_no"],
))
register(DocType(
    slug="account", model=Account, module="accounts",
    list_fields=["number", "name", "parent", "root_type", "account_type", "is_group"],
    form_fields=["number", "name", "parent", "is_group", "root_type", "account_type", "disabled"],
    search_fields=["number", "name"],
))
