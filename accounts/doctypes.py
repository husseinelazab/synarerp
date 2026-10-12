from accounts.models import (Account, Budget, DeferralSchedule, ExchangeRevaluation, ExchangeRevaluationLine, BudgetLine, Cheque, BankTransaction, CostCenter, JournalEntry, JournalEntryAccount, OpeningAccountBalance, OpeningAccountLine,
                             OpeningPartyBalance, OpeningPartyLine, PaymentEntry, PaymentTermLine, PaymentTermsTemplate, PaymentReconciliation,
                             PaymentReconciliationLine, PaymentReference, PeriodClosingVoucher)
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
    child=ChildTable(PaymentReference, ["sales_invoice", "purchase_invoice", "allocated_amount"],
                     label="التخصيص على الفواتير", min_rows=0,
                     help="لتوزيع السند على عدة فواتير أضف سطراً لكل فاتورة بالمبلغ المخصص لها، أو احفظ السند ثم "
                          "اضغط «توزيع تلقائي على أقدم الفواتير». ما يتبقى من المبلغ دون تخصيص يُسجل دفعة مقدمة "
                          "تُسوّى لاحقاً من «تسوية الدفعات»."),
    totals=["amount", "allocated_amount", "unallocated_amount"],
))
register(DocType(
    slug="payment-reconciliation", model=PaymentReconciliation, module="accounts",
    list_fields=["name", "posting_date", "party_type", "customer", "supplier", "total_allocated"],
    form_fields=["party_type", "posting_date", "customer", "supplier", "remarks"],
    search_fields=["name", "customer__name", "supplier__name"],
    child=ChildTable(PaymentReconciliationLine, ["payment", "sales_return", "purchase_return", "sales_invoice",
                                                 "purchase_invoice", "amount"], label="سطور التسوية"),
    totals=["total_allocated", "exchange_difference"],
))
register(DocType(
    slug="account", model=Account, module="accounts",
    list_fields=["number", "name", "parent", "root_type", "account_type", "is_group"],
    form_fields=["number", "name", "parent", "is_group", "root_type", "account_type", "cash_flow_category", "disabled"],
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
register(DocType(
    slug="payment-terms", model=PaymentTermsTemplate, module="accounts",
    list_fields=["name", "disabled"], form_fields=["name", "disabled"], search_fields=["name"],
    child=ChildTable(PaymentTermLine, ["description", "invoice_portion", "credit_days"], label="الأقساط",
                     help="مجموع النسب 100%. مثال: 30% بعد 0 يوم (مقدم)، ثم 35% بعد 30 يوماً، ثم 35% بعد 60 يوماً."),
))
register(DocType(
    slug="cheque", model=Cheque, module="accounts",
    list_fields=["name", "direction", "cheque_no", "customer", "supplier", "cheque_date", "amount", "status"],
    form_fields=["direction", "posting_date", "customer", "supplier", "cheque_no", "drawer_bank", "cheque_date",
                 "amount", "bank_account", "sales_invoice", "purchase_invoice", "remarks"],
    search_fields=["name", "cheque_no", "customer__name", "supplier__name", "drawer_bank"],
    totals=["amount"],
))
register(DocType(
    slug="budget", model=Budget, module="accounts",
    list_fields=["name", "fiscal_year", "cost_center", "control_period", "action", "disabled"],
    form_fields=["name", "fiscal_year", "cost_center", "control_period", "action", "disabled"],
    search_fields=["name"],
    child=ChildTable(BudgetLine, ["account", "amount"], label="بنود الموازنة"),
))
register(DocType(
    slug="exchange-revaluation", model=ExchangeRevaluation, module="accounts",
    list_fields=["name", "posting_date", "total_gain"], form_fields=["posting_date", "remarks"],
    search_fields=["name", "remarks"],
    child=ChildTable(ExchangeRevaluationLine, ["new_rate"], label="الأرصدة المعاد تقييمها", min_rows=0,
                     display_fields=["sales_invoice", "purchase_invoice", "currency", "outstanding", "invoice_rate",
                                     "new_rate", "difference"],
                     help="احفظ المستند فارغاً لتُجلب الفواتير المفتوحة بالعملات الأجنبية بسعر صرف تاريخه من جدول أسعار الصرف."),
    totals=["total_gain"],
))
register(DocType(
    slug="deferral", model=DeferralSchedule, module="accounts",
    list_fields=["name", "kind", "pl_account", "amount", "start_date", "months", "posted_amount"],
    form_fields=["kind", "posting_date", "balance_account", "pl_account", "cost_center", "amount", "start_date", "months",
                 "remarks"],
    search_fields=["name", "remarks"], totals=["amount", "posted_amount"],
    detail_template="accounts/_deferral_entries.html",
))
