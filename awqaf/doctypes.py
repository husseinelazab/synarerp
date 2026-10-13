from awqaf.models import RentInstallment, RentReceipt, Waqf, WaqfDistribution, WaqfLease, WaqfShare
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="waqf", model=Waqf, module="awqaf",
    list_fields=["name", "waqf_type", "waqif", "deed_number", "asset_value", "is_active"],
    search_fields=["name", "waqif", "deed_number"],
    child=ChildTable(WaqfShare, ["name", "percent", "target_fund"], label="مصارف الغلة (شرط الواقف)", min_rows=0,
                     help="مجموع النسب 100%. مثال: النظارة 10% (فارغ = عام)، عمارة الوقف 20% (صندوق احتياطي)، "
                          "الأيتام 70% (صندوق الكفالات)."),
    detail_template="awqaf/_waqf_detail.html",
))
register(DocType(
    slug="waqf-lease", model=WaqfLease, module="awqaf",
    list_fields=["waqf", "unit", "tenant_name", "start_date", "end_date", "annual_rent", "frequency", "status"],
    search_fields=["unit", "tenant_name", "tenant_phone", "contract_number", "waqf__name"],
    detail_template="awqaf/_lease_detail.html",
))
register(DocType(
    slug="rent-installment", model=RentInstallment, module="awqaf",
    list_fields=["lease", "due_date", "amount", "collected_amount", "status"],
    search_fields=["lease__unit", "lease__tenant_name", "lease__waqf__name"], show_in_menu=False,
))
register(DocType(
    slug="rent-receipt", model=RentReceipt, module="awqaf",
    list_fields=["name", "posting_date", "lease", "amount", "account"],
    form_fields=["lease", "installment", "posting_date", "amount", "account", "reference_no", "remarks"],
    search_fields=["name", "lease__tenant_name", "lease__unit", "reference_no"],
))
register(DocType(
    slug="waqf-distribution", model=WaqfDistribution, module="awqaf",
    list_fields=["name", "posting_date", "waqf", "amount"],
    form_fields=["waqf", "posting_date", "amount", "remarks"],
    search_fields=["name", "waqf__name"], detail_template="awqaf/_distribution_detail.html",
))
