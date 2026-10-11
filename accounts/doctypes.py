from accounts.models import (Account, BankTransaction, Budget, BudgetLine, CostCenter, ExchangeRevaluation, ExchangeRevaluationLine, JournalEntry,
                             JournalEntryAccount, OpeningAccountBalance, OpeningAccountLine, OpeningPartyBalance, OpeningPartyLine,
                             PaymentEntry, PeriodClosingVoucher)
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="journal-entry", model=JournalEntry, module="accounts",
    list_fields=["name", "posting_date", "entry_type", "total_debit", "remarks"],
    form_fields=["posting_date", "entry_type", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(JournalEntryAccount, ["account", "customer", "supplier", "debit", "credit", "cost_center", "description",
                                           "account_currency_amount"],
                     label="الحسابات"),
    totals=["total_debit", "total_credit"],
))
register(DocType(
    slug="payment-entry", model=PaymentEntry, module="accounts",
    list_fields=["name", "posting_date", "payment_type", "customer", "supplier", "amount"],
    form_fields=["payment_type", "posting_date", "customer", "supplier", "account", "amount", "currency", "exchange_rate",
                 "sales_invoice", "purchase_invoice", "payment_mode", "cheque_no", "cheque_date", "drawee_bank",
                 "reference_no", "remarks"],
    detail_template="accounts/_payment_cheque.html",
    search_fields=["name", "customer__name", "supplier__name", "reference_no"],
))
register(DocType(
    slug="account", model=Account, module="accounts",
    list_fields=["number", "name", "parent", "root_type", "account_type", "is_group"],
    form_fields=["number", "name", "parent", "is_group", "root_type", "account_type", "account_currency", "cash_flow_category",
                 "disabled"],
    search_fields=["number", "name"],
))
register(DocType(
    slug="opening-account", model=OpeningAccountBalance, module="accounts",
    list_fields=["name", "posting_date", "remarks"], form_fields=["posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(OpeningAccountLine, ["account", "debit", "credit", "description", "account_currency_amount"],
                     label="الحسابات"),
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
    slug="exchange-revaluation", model=ExchangeRevaluation, module="accounts",
    list_fields=["name", "posting_date", "total_difference", "remarks"], form_fields=["posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(ExchangeRevaluationLine, ["account", "new_rate"], label="الحسابات بعملة أجنبية", min_rows=0,
                     display_fields=["account", "balance_in_account_currency", "book_balance", "new_rate",
                                     "revalued_balance", "difference"],
                     help="اترك الجدول فارغاً واحفظ لتُجلب حسابات البنك والصندوق الأجنبية بأرصدتها حتى التاريخ "
                          "وآخر سعر صرف مسجل. يمكنك تعديل السعر قبل الاعتماد، والفرق يُرحّل لحساب فروق أسعار الصرف."),
    totals=["total_difference"],
))
register(DocType(
    slug="budget", model=Budget, module="accounts",
    list_fields=["name", "fiscal_year", "cost_center", "action", "total_amount"],
    form_fields=["fiscal_year", "cost_center", "action", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(BudgetLine, ["account", "amount", "notes"], label="حسابات المصروفات",
                     help="المبلغ السنوي لكل حساب مصروف؛ يُوزع بالتساوي على الأشهر في تقرير «الموازنة مقابل الفعلي». "
                          "الرقابة (التنبيه أو المنع) تقارن مجموع مصروف السنة بالموازنة السنوية."),
    totals=["total_amount"],
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
