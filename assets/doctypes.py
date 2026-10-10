from assets.models import Asset, AssetCategory
from core.registry import DocType, register

register(DocType(
    slug="asset", model=Asset, module="accounts",
    list_fields=["name", "asset_name", "category", "purchase_date", "gross_amount", "accumulated_depreciation", "status"],
    form_fields=["asset_name", "category", "serial_no", "location", "custodian", "cost_center", "purchase_date",
                 "gross_amount", "salvage_value", "useful_life_months", "depreciation_start_date",
                 "opening_accumulated_depreciation", "credit_account", "supplier", "posting_date", "remarks"],
    search_fields=["name", "asset_name", "serial_no", "location"],
    detail_template="assets/_asset_detail.html",
    detail_exclude=["accumulated_depreciation", "disposal_date", "disposal_entry"],
))
register(DocType(
    slug="asset-category", model=AssetCategory, module="accounts",
    list_fields=["name", "asset_account", "useful_life_months"], search_fields=["name"],
))
