from core.registry import ChildTable, DocType, register
from grants.models import Grant, GrantApplication, GrantBudgetLine, GrantInstallment, GrantReport

register(DocType(
    slug="grant-application", model=GrantApplication, module="grants",
    list_fields=["title", "grantor", "requested_amount", "submitted_date", "status", "decision_date"],
    search_fields=["title", "grantor__name", "notes"],
))
register(DocType(
    slug="grant", model=Grant, module="grants",
    list_fields=["title", "grantor", "agreement_number", "approved_amount", "start_date", "end_date", "fund",
                 "status"],
    search_fields=["title", "agreement_number", "grantor__name"],
    child=ChildTable(GrantBudgetLine, ["description", "account", "amount"], label="موازنة المنحة", fk_name="grant",
                     min_rows=0, help="بنود الموازنة المعتمدة من المانح، ولكل بند حساب المصروف الذي يُقيد عليه الصرف "
                                      "ببعد صندوق المنحة."),
    detail_template="grants/_grant_detail.html",
))
register(DocType(
    slug="grant-installment", model=GrantInstallment, module="grants",
    list_fields=["grant", "due_date", "amount", "receipt"],
    search_fields=["grant__title", "grant__grantor__name", "notes"],
))
register(DocType(
    slug="grant-report", model=GrantReport, module="grants",
    list_fields=["grant", "kind", "period_start", "period_end", "due_date", "status", "submitted_date"],
    search_fields=["grant__title", "grant__grantor__name", "notes"],
))
