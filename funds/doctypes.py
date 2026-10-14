from core.registry import ChildTable, DocType, register
from funds.models import (DonationReceipt, Donor, InKindDonation, InKindDonationItem, Sponsorship, SponsorshipDue,
                          SponsorshipType)

register(DocType(
    slug="donation-receipt", model=DonationReceipt, module="resources",
    list_fields=["name", "posting_date", "donor_name", "donation_type", "fund", "amount", "payment_method"],
    form_fields=["posting_date", "donor", "donor_name", "donor_phone", "donation_type", "fund", "amount",
                 "payment_method", "account", "reference_no", "income_account", "remarks"],
    search_fields=["name", "donor_name", "donor_phone", "reference_no", "donor__name"],
))
register(DocType(
    slug="donor", model=Donor, module="resources",
    list_fields=["name", "donor_type", "phone", "city", "is_active"],
    search_fields=["name", "phone", "email"], detail_template="funds/_donor_detail.html",
))
register(DocType(
    slug="sponsorship-type", model=SponsorshipType, module="resources",
    list_fields=["name", "monthly_amount", "aid_program"], search_fields=["name"],
))
register(DocType(
    slug="sponsorship", model=Sponsorship, module="resources",
    list_fields=["number", "donor", "beneficiary", "sponsorship_type", "monthly_amount", "start_date", "end_date",
                 "status"],
    search_fields=["number", "donor__name", "beneficiary__full_name", "beneficiary__file_number"],
    detail_template="funds/_sponsorship_detail.html",
))
register(DocType(
    slug="sponsorship-due", model=SponsorshipDue, module="resources",
    list_fields=["sponsorship", "period", "amount", "collected_amount", "status", "disbursement"],
    search_fields=["sponsorship__number", "sponsorship__donor__name", "sponsorship__beneficiary__full_name"],
    show_in_menu=False,
))
register(DocType(
    slug="in-kind-donation", model=InKindDonation, module="resources",
    list_fields=["name", "posting_date", "donor_name", "restriction", "warehouse", "total"],
    form_fields=["posting_date", "donor", "donor_name", "restriction", "fund", "warehouse", "income_account", "remarks"],
    search_fields=["name", "donor_name", "donor__name"],
    child=ChildTable(InKindDonationItem, ["item", "qty", "rate"], label="الأصناف المتبرع بها",
                     display_fields=["item", "qty", "rate", "amount"],
                     help="القيمة العادلة: سعر السوق للصنف عند الاستلام (تُثبت بها في المخزون والإيراد)."),
))
